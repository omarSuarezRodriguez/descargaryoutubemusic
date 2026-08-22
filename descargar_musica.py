"""
Descargador de audio desde YouTube Music / YouTube.
Pega varios enlaces (uno por línea) y descarga MP3 u Opus.
Hasta 2 descargas en paralelo (misma calidad; solo velocidad).
"""

from __future__ import annotations

import json
import hashlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import download_state as dlstate
from metadata_extras import attach_lyrics_and_cover
from link_catalog import LinkCatalog, refresh_link_catalog_window, show_link_catalog_window

AUDIO_EXTENSIONS = {".mp3", ".webm", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
# Solo velocidad: 1 pista a la vez (no afecta calidad/formato)
PARALLEL_DOWNLOADS = 1
ID_NAME_CACHE = ".yt_id_names.json"
STAGING_DIRNAME = ".staging"


def find_yt_dlp() -> list[str]:
    """Devuelve el comando base para invocar yt-dlp."""
    # Preferir el módulo de Python (más fiable en Windows)
    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        pass

    # Fallback: ejecutable en PATH
    return ["yt-dlp"]


def yt_dlp_js_runtime_args() -> list[str]:
    """
    Runtime JS para extracción YouTube (yt-dlp EJS).
    No cambia calidad/formato; solo fiabilidad al listar/descargar.
    """
    from shutil import which

    if which("node"):
        return ["--js-runtimes", "node"]
    return []


def yt_dlp_remote_components_args() -> list[str]:
    """Script EJS remoto (challenge JS). No cambia bestaudio/formato."""
    return ["--remote-components", "ejs:github"]


def yt_dlp_cookies_args(cookies_file: str | Path | None = None) -> list[str]:
    """
    --cookies si hay archivo. Lee prefs compartidas si no se pasa ruta.
    No cambia calidad/formato.
    """
    path: Path | None = None
    if cookies_file is not None and str(cookies_file).strip():
        path = Path(str(cookies_file).strip()).expanduser()
        try:
            path = path.resolve()
        except OSError:
            path = None
        if path is not None and not path.is_file():
            path = None
    else:
        path = dlstate.get_cookies_path()
    if path is None:
        return []
    return ["--cookies", str(path)]


def default_cookies_suggestion() -> str:
    """Sugiere Desktop/cookies.txt si existe; si no, la preferencia guardada."""
    saved = dlstate.get_cookies_path_text()
    if saved:
        return saved
    desktop = Path.home() / "Desktop" / "cookies.txt"
    if desktop.is_file():
        return str(desktop)
    return ""


def is_transient_yt_block(text: str) -> bool:
    """True si el fallo parece bloqueo temporal de YouTube (bot/rate), no formato."""
    t = (text or "").lower()
    needles = (
        "cookies",
        "sign in to confirm",
        "not a bot",
        "http error 429",
        "too many requests",
        "please confirm that you are not a bot",
    )
    return any(n in t for n in needles)


def find_ffmpeg() -> Path | None:
    """Localiza ffmpeg (PATH o instalaciones típicas de WinGet en Windows)."""
    from shutil import which

    in_path = which("ffmpeg")
    if in_path:
        return Path(in_path).resolve()

    local = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if local.is_dir():
        matches = sorted(
            local.glob("**/ffmpeg.exe"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return matches[0].resolve()

    for candidate in (
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path.home() / "scoop" / "shims" / "ffmpeg.exe",
        Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
    ):
        if candidate.is_file():
            return candidate.resolve()

    return None


def clean_filename(name: str) -> str:
    """Quita caracteres inválidos en Windows y deja el nombre limpio."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.replace("\u200b", "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "audio"


# YT Music suele titular playlists de álbum como "Album - NombreReal"
_ALBUM_TYPE_PREFIX_RE = re.compile(
    r"^(?:Album|Álbum|EP|Single|Playlist|Compilación|Compilation)\s*[-–—:]\s+",
    re.IGNORECASE,
)


def normalize_album_folder_name(album: str) -> str:
    """Quita prefijos tipo 'Album - …' dejando solo el nombre del álbum."""
    name = (album or "").strip()
    if not name:
        return name
    cleaned = _ALBUM_TYPE_PREFIX_RE.sub("", name, count=1).strip()
    return cleaned or name


def song_artist_basename(info: dict) -> str:
    """Nombre limpio: 'canción - artista'."""
    track = (info.get("track") or "").strip()
    artist = (
        info.get("artist")
        or info.get("album_artist")
        or info.get("creator")
        or ""
    )
    if isinstance(artist, list):
        artist = ", ".join(str(a) for a in artist if a)
    artist = str(artist).strip()
    title = (info.get("title") or "").strip()

    if track and artist:
        return clean_filename(f"{track} - {artist}")

    if artist and title:
        song = title
        prefix = f"{artist} - "
        if title.casefold().startswith(prefix.casefold()):
            song = title[len(prefix) :].strip()
        return clean_filename(f"{song} - {artist}")

    # Título típico de YT Music: "Artista - Canción"
    if " - " in title:
        left, right = title.split(" - ", 1)
        return clean_filename(f"{right.strip()} - {left.strip()}")

    return clean_filename(title or track or "audio")


def folder_artist_from_info(info: dict) -> str:
    """Artista para carpeta (sin caracteres inválidos)."""
    artist = (
        info.get("artist")
        or info.get("album_artist")
        or info.get("creator")
        or ""
    )
    if isinstance(artist, list):
        artist = ", ".join(str(a) for a in artist if a)
    artist = str(artist).strip()
    if artist.casefold().endswith(" - topic"):
        artist = artist[: -len(" - Topic")].strip()
    if not artist:
        title = (info.get("title") or "").strip()
        if " - " in title:
            artist = title.split(" - ", 1)[0].strip()
        else:
            artist = (info.get("uploader") or info.get("channel") or "").strip()
            if artist.casefold().endswith(" - topic"):
                artist = artist[: -len(" - Topic")].strip()
    return clean_filename(artist) or "Artista desconocido"


def folder_album_from_info(info: dict) -> str:
    """Álbum para carpeta; si falta → 'Sin álbum'."""
    album = info.get("album") or ""
    if isinstance(album, list):
        album = str(album[0]).strip() if album else ""
    album = normalize_album_folder_name(str(album).strip())
    if not album:
        return "Sin álbum"
    return clean_filename(album) or "Sin álbum"


def ensure_artist_album_dir(base_dir: Path, artist: str, album: str) -> Path:
    """
    Destino: base / Artista / NombreAlbum /
    Si existe, reutiliza; si no, crea (parents=True, exist_ok=True).
    """
    artist_dir = clean_filename(artist) or "Artista desconocido"
    album_dir = clean_filename(normalize_album_folder_name(album)) or "Sin álbum"
    dest = base_dir / artist_dir / album_dir
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def find_existing_download(out_dir: Path, basename: str) -> Path | None:
    """Busca si ya hay un archivo con ese nombre (cualquier extensión de audio)."""
    if not out_dir.is_dir():
        return None
    target = basename.casefold()
    for path in out_dir.iterdir():
        if (
            path.is_file()
            and path.suffix.lower() in AUDIO_EXTENSIONS
            and path.stem.casefold() == target
        ):
            return path
    return None


def fetch_video_info(base_cmd: list[str], url: str) -> dict:
    """Obtiene metadatos sin descargar (solo usos puntuales, p. ej. enriquecer álbum)."""
    cmd = [
        *base_cmd,
        *yt_dlp_js_runtime_args(),
        *yt_dlp_remote_components_args(),
        *yt_dlp_cookies_args(),
        "--skip-download",
        "--no-playlist",
        "-J",
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail[-500:] or f"código {result.returncode}")
    return json.loads(result.stdout)


def extract_youtube_id(url: str) -> str | None:
    """ID de vídeo desde URL watch/youtu.be/shorts (si se puede)."""
    m = re.search(
        r"(?:youtu\.be/|v=|/shorts/|/embed/)([\w-]{6,})",
        url,
        re.IGNORECASE,
    )
    return m.group(1) if m else None


def build_download_cmd(
    base_cmd: list[str],
    url: str,
    out_dir: Path,
    format_mode: str,
    ffmpeg: Path | None,
    basename: str | None = None,
    *,
    staging: bool = False,
) -> list[str]:
    """
    Arma yt-dlp. Si staging=True: salida %(id)s + --write-info-json
    (metadatos en/tras descarga; sin -J previo).
    """
    if staging:
        out_tmpl = str(out_dir / "%(id)s.%(ext)s")
    else:
        if not basename:
            raise ValueError("basename requerido si staging=False")
        out_tmpl = str(out_dir / f"{basename}.%(ext)s")

    cmd = [
        *base_cmd,
        *yt_dlp_js_runtime_args(),
        *yt_dlp_remote_components_args(),
        *yt_dlp_cookies_args(),
        "-f",
        "bestaudio",
        "-N",
        "1",
        "-o",
        out_tmpl,
        "--no-playlist",
    ]
    if staging:
        cmd.append("--write-info-json")
    if format_mode in {"opus", "webm"}:
        pass
    else:
        cmd.extend(
            [
                "-x",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "0",
            ]
        )
        if ffmpeg:
            cmd.extend(["--ffmpeg-location", str(ffmpeg.parent)])
    cmd.append(url)
    return cmd


def load_info_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def find_info_json(staging_dir: Path, video_id: str | None = None) -> Path | None:
    if video_id:
        p = staging_dir / f"{video_id}.info.json"
        if p.is_file():
            return p
    jsons = sorted(
        staging_dir.glob("*.info.json"),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    return jsons[0] if jsons else None


def find_staged_audio(staging_dir: Path, video_id: str | None) -> Path | None:
    if not staging_dir.is_dir():
        return None
    if video_id:
        for path in staging_dir.iterdir():
            if (
                path.is_file()
                and path.stem == video_id
                and path.suffix.lower() in AUDIO_EXTENSIONS
            ):
                return path
    # Fallback: audio más reciente (sin .info.json)
    audios = [
        p
        for p in staging_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not audios:
        return None
    audios.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return audios[0]


def cleanup_staging_id(staging_dir: Path, video_id: str | None) -> None:
    if not video_id or not staging_dir.is_dir():
        return
    for path in list(staging_dir.iterdir()):
        if path.is_file() and (
            path.stem == video_id or path.name.startswith(f"{video_id}.")
        ):
            try:
                path.unlink()
            except OSError:
                pass
    remove_staging_if_empty(staging_dir)


def remove_staging_if_empty(staging_dir: Path) -> None:
    """Borra la carpeta staging si quedó vacía."""
    if not staging_dir.is_dir():
        return
    try:
        if any(staging_dir.iterdir()):
            return
        staging_dir.rmdir()
    except OSError:
        pass


def scrub_delivery_artifacts(folder: Path) -> None:
    """
    Quita residuos internos de carpetas de entrega (.yt_id_names.json, .staging).
    No toca audio ni metadatos embebidos.
    """
    if not folder.is_dir():
        return
    legacy_cache = folder / ID_NAME_CACHE
    if legacy_cache.is_file():
        try:
            legacy_cache.unlink()
        except OSError:
            pass
    legacy_staging = folder / STAGING_DIRNAME
    if legacy_staging.is_dir():
        try:
            shutil.rmtree(legacy_staging, ignore_errors=True)
        except OSError:
            pass


def _path_key(path: Path) -> str:
    try:
        raw = str(path.resolve())
    except OSError:
        raw = str(path)
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def get_staging_dir(parent: Path) -> Path:
    """
    Staging fuera de la carpeta de música (.download_queue/staging/...).
    Así las carpetas de canciones no acumulan .staging.
    """
    queue_dir = dlstate.ensure_queue_dir()
    staging = queue_dir / "staging" / _path_key(parent)
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def promote_staged_to_dest(
    staging_dir: Path,
    dest_dir: Path,
    info: dict,
) -> tuple[str, Path | None, str]:
    """
    Mueve audio de staging a dest_dir como 'canción - artista.ext'.
    Devuelve (ok|skipped|fail, path_final|None, basename).
    """
    basename = song_artist_basename(info)
    video_id = str(info.get("id") or "") or None
    try:
        if find_existing_download(dest_dir, basename):
            return "skipped", None, basename
        audio = find_staged_audio(staging_dir, video_id)
        if audio is None:
            return "fail", None, basename
        dest_dir.mkdir(parents=True, exist_ok=True)
        final_path = dest_dir / f"{basename}{audio.suffix.lower()}"
        if final_path.exists():
            return "skipped", None, basename
        audio.replace(final_path)
        return "ok", final_path, basename
    finally:
        cleanup_staging_id(staging_dir, video_id)
        scrub_delivery_artifacts(dest_dir)


def _cache_path(base_dir: Path) -> Path:
    """Caché id→nombre en .download_queue (no junto a las canciones)."""
    queue_dir = dlstate.ensure_queue_dir()
    cache_root = queue_dir / "yt_id_names"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root / f"{_path_key(base_dir)}.json"


def _legacy_cache_path(base_dir: Path) -> Path:
    return base_dir / ID_NAME_CACHE


def load_id_name_cache(base_dir: Path) -> dict[str, dict[str, str]]:
    path = _cache_path(base_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # Migrar caché antigua que vivía en la carpeta de música
    legacy = _legacy_cache_path(base_dir)
    if legacy.is_file():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                save_id_name_cache(base_dir, data)
                try:
                    legacy.unlink()
                except OSError:
                    pass
                return data
        except Exception:
            pass
    return {}


def save_id_name_cache(base_dir: Path, cache: dict[str, dict[str, str]]) -> None:
    try:
        _cache_path(base_dir).write_text(
            json.dumps(cache, ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    except OSError:
        pass
    # Por si quedó el archivo viejo junto a las canciones
    scrub_delivery_artifacts(base_dir)


def cache_lookup_existing(
    base_dir: Path,
    video_id: str | None,
    *,
    fixed_dest: Path | None = None,
) -> tuple[str, Path] | None:
    """Si hay caché del id y el archivo ya existe → (basename, path)."""
    if not video_id:
        return None
    cache = load_id_name_cache(base_dir)
    entry = cache.get(video_id)
    if not isinstance(entry, dict):
        return None
    basename = (entry.get("basename") or "").strip()
    if not basename:
        return None
    if fixed_dest is not None:
        dest = fixed_dest
    else:
        artist = entry.get("artist") or "Artista desconocido"
        album = entry.get("album") or "Sin álbum"
        dest = base_dir / clean_filename(artist) / clean_filename(album)
    existing = find_existing_download(dest, basename)
    if existing:
        return basename, existing
    return None


def cache_remember(
    base_dir: Path,
    video_id: str | None,
    basename: str,
    artist: str,
    album: str,
) -> None:
    if not video_id or not basename:
        return
    cache = load_id_name_cache(base_dir)
    cache[video_id] = {
        "basename": basename,
        "artist": artist,
        "album": album,
    }
    save_id_name_cache(base_dir, cache)


def parse_urls(text: str) -> list[str]:
    """Extrae URLs válidas de YouTube / YouTube Music, una por línea o separadas."""
    urls: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"https?://(?:www\.|music\.)?youtube\.com/\S+|https?://youtu\.be/\S+",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for url in pattern.findall(line):
            url = url.rstrip(".,);]")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def ask_cancel_download(parent: tk.Misc, detail: str | None = None) -> bool:
    """Diálogo: ¿cancelar la descarga en curso? True = sí cancelar."""
    msg = detail or (
        "Hay una descarga en curso.\n\n¿Desea cancelar la descarga?"
    )
    return bool(
        messagebox.askyesno(
            "Cancelar descarga",
            msg,
            parent=parent,
            icon="warning",
        )
    )


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Descargar YouTube Music")
        self.geometry("720x560")
        self.minsize(560, 420)

        self.download_dir = tk.StringVar(
            value=str(Path.home() / "Downloads" / "YouTubeMusic" / "Canciones")
        )
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._ffmpeg = find_ffmpeg()
        self._busy = False
        self._last_clip: str | None = None
        self.clipboard_watch = tk.BooleanVar(value=True)
        self.format_mode = tk.StringVar(value="mp3")
        self.cookies_file = tk.StringVar(value=default_cookies_suggestion())
        self._link_catalog = LinkCatalog()
        self._active_batch_id: str | None = None
        self._poll_after_id: str | None = None
        self._log_offset = 0
        self._event_lines: list[str] = []
        self._registry_text: str | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_log_queue)
        self.after(200, self._check_deps)
        self.after(400, self._poll_clipboard)
        self.after(600, self._resume_background_if_needed)
        # Persistir sugerencia inicial para el worker si el archivo existe
        self._sync_cookies_prefs()

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style(self)
        style.configure(
            "Speed.TLabel",
            foreground="#1a7f37",
            font=("Segoe UI", 11, "bold"),
        )

        header = ttk.Frame(root)
        header.pack(fill=tk.X, **pad)
        ttk.Label(
            header,
            text="Enlaces (uno por línea). Si copias un enlace, se añade solo:",
        ).pack(side=tk.LEFT, anchor=tk.W)
        self.lbl_speed = ttk.Label(
            header,
            text="",
            style="Speed.TLabel",
            anchor=tk.E,
            width=14,
        )
        self.lbl_speed.pack(side=tk.RIGHT, anchor=tk.E)

        self.txt_urls = scrolledtext.ScrolledText(root, height=12, wrap=tk.WORD)
        self.txt_urls.pack(fill=tk.BOTH, expand=True, **pad)

        opts_row = ttk.Frame(root)
        opts_row.pack(fill=tk.X, **pad)
        ttk.Checkbutton(
            opts_row,
            text="Pegar automáticamente del portapapeles",
            variable=self.clipboard_watch,
        ).pack(side=tk.LEFT)

        format_row = ttk.Frame(root)
        format_row.pack(fill=tk.X, **pad)
        ttk.Label(format_row, text="Formato:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            format_row,
            text="MP3 (compatible)",
            variable=self.format_mode,
            value="mp3",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Radiobutton(
            format_row,
            text="Opus (tal cual YouTube)",
            variable=self.format_mode,
            value="opus",
        ).pack(side=tk.LEFT, padx=(12, 0))

        folder_row = ttk.Frame(root)
        folder_row.pack(fill=tk.X, **pad)
        ttk.Label(folder_row, text="Carpeta:").pack(side=tk.LEFT)
        ttk.Entry(folder_row, textvariable=self.download_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        ttk.Button(folder_row, text="Elegir…", command=self._choose_folder).pack(
            side=tk.LEFT
        )

        cookies_row = ttk.Frame(root)
        cookies_row.pack(fill=tk.X, **pad)
        ttk.Label(cookies_row, text="Cookies:").pack(side=tk.LEFT)
        ttk.Entry(cookies_row, textvariable=self.cookies_file).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        ttk.Button(
            cookies_row, text="Elegir…", command=self._choose_cookies
        ).pack(side=tk.LEFT)
        ttk.Button(
            cookies_row, text="Quitar", command=self._clear_cookies
        ).pack(side=tk.LEFT, padx=(8, 0))

        btn_row = ttk.Frame(root)
        btn_row.pack(fill=tk.X, **pad)
        self.btn_download = ttk.Button(
            btn_row, text="Descargar", command=self._start_download
        )
        self.btn_download.pack(side=tk.LEFT)
        self.btn_stop = ttk.Button(
            btn_row, text="Detener", command=self._stop_download, state=tk.DISABLED
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(
            btn_row, text="Listado…", command=self._open_link_list
        ).pack(side=tk.LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(btn_row, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 8))
        self.progress_pct = ttk.Label(btn_row, text="0%", width=5, anchor=tk.E)
        self.progress_pct.pack(side=tk.LEFT)

        eta_row = ttk.Frame(root)
        eta_row.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.lbl_eta = ttk.Label(eta_row, text="", anchor=tk.E)
        self.lbl_eta.pack(side=tk.RIGHT)

        ttk.Label(root, text="Registro:").pack(anchor=tk.W, padx=12, pady=(8, 0))
        self.txt_log = scrolledtext.ScrolledText(
            root, height=10, wrap=tk.WORD, state=tk.DISABLED
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, **pad)

    def _open_link_list(self) -> None:
        show_link_catalog_window(self, self._link_catalog)

    def _choose_folder(self) -> None:
        path = filedialog.askdirectory(initialdir=self.download_dir.get())
        if path:
            self.download_dir.set(path)

    def _choose_cookies(self) -> None:
        initial = self.cookies_file.get().strip()
        initial_dir = str(Path(initial).parent) if initial else str(Path.home() / "Desktop")
        path = filedialog.askopenfilename(
            title="Elegir cookies.txt",
            initialdir=initial_dir,
            filetypes=[
                ("Cookies Netscape", "*.txt"),
                ("Todos los archivos", "*.*"),
            ],
        )
        if path:
            self.cookies_file.set(path)
            self._sync_cookies_prefs(announce=True)

    def _clear_cookies(self) -> None:
        self.cookies_file.set("")
        self._sync_cookies_prefs(announce=True)

    def _sync_cookies_prefs(self, *, announce: bool = False) -> None:
        """Guarda la ruta de cookies para la UI y el worker en segundo plano."""
        text = self.cookies_file.get().strip()
        dlstate.set_cookies_path(text or None)
        if not announce:
            return
        path = Path(text) if text else None
        if not text:
            self._log("Cookies: (ninguno)")
        elif path is None or not path.is_file():
            self._log(f"AVISO: cookies no encontradas: {text}")
        else:
            self._log(f"Cookies: {text}")

    def _read_clipboard(self) -> str:
        try:
            return self.clipboard_get().strip()
        except tk.TclError:
            return ""

    def append_urls_from_text(self, text: str) -> list[str]:
        """Añade URLs nuevas a la caja. Devuelve las que se agregaron."""
        if self._busy or str(self.txt_urls.cget("state")) == "disabled":
            return []

        candidates = parse_urls(text)
        if not candidates:
            return []

        existing = set(parse_urls(self.txt_urls.get("1.0", tk.END)))
        added: list[str] = []
        for url in candidates:
            if url in existing:
                continue
            content = self.txt_urls.get("1.0", tk.END)
            if content and not content.endswith("\n"):
                self.txt_urls.insert(tk.END, "\n")
            self.txt_urls.insert(tk.END, url + "\n")
            existing.add(url)
            added.append(url)

        if added:
            self.txt_urls.see(tk.END)
        return added

    def _poll_clipboard(self) -> None:
        try:
            clip = self._read_clipboard()
            if self._last_clip is None:
                # Primera lectura: solo memorizar, no pegar lo que ya había
                self._last_clip = clip
            elif not self.clipboard_watch.get() or self._busy:
                # Mantener sincronizado para no volcar cambios al reactivar
                self._last_clip = clip
            elif clip and clip != self._last_clip:
                self._last_clip = clip
                added = self.append_urls_from_text(clip)
                for url in added:
                    self._log(f"Portapapeles → {url}")
            elif clip != self._last_clip:
                self._last_clip = clip
        finally:
            self.after(500, self._poll_clipboard)

    def _check_deps(self) -> None:
        if self._ffmpeg:
            self._log(f"ffmpeg: {self._ffmpeg}")
        else:
            self._log(
                "AVISO: no se encontró ffmpeg. Hace falta para MP3 y para Opus.\n"
                "Instálalo (winget install Gyan.FFmpeg) o agrégalo al PATH."
            )

    def _log(self, message: str) -> None:
        self._log_queue.put(message)

    def _clear_download_log(self) -> None:
        """Reinicia Registro mínimo."""
        self._event_lines = []
        self._registry_text = None
        self._log_offset = len(dlstate.read_worker_log_tail(10_000))
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _drain_log_queue(self) -> None:
        """Mensajes puntuales (cancelar/error) → eventos o líneas sueltas."""
        new_parts: list[str] = []
        while True:
            try:
                message = self._log_queue.get_nowait()
            except queue.Empty:
                break
            for part in str(message).splitlines():
                part = part.strip()
                if part:
                    new_parts.append(part)
        if new_parts:
            if self._busy:
                self._event_lines.extend(new_parts)
                self._event_lines = self._event_lines[-40:]
                try:
                    live = dlstate.read_live_progress()
                except Exception:
                    live = []
                self._rebuild_registry(
                    live,
                    finished=getattr(self, "_last_finished", 0),
                    total=getattr(self, "_last_total", 1),
                    active=True,
                )
            else:
                self.txt_log.configure(state=tk.NORMAL)
                for part in new_parts:
                    self.txt_log.insert(tk.END, part + "\n")
                self.txt_log.see(tk.END)
                self.txt_log.configure(state=tk.DISABLED)
        self.after(100, self._drain_log_queue)

    def _ingest_worker_events(self) -> None:
        """Incorpora solo eventos útiles del worker (✓/⊘/✗)."""
        try:
            tail = dlstate.read_worker_log_tail(10_000)
        except Exception:
            return
        if len(tail) < self._log_offset:
            self._log_offset = 0
        for line in tail[self._log_offset :]:
            ev = dlstate.summarize_worker_event(line)
            if ev:
                self._event_lines.append(ev)
        self._log_offset = len(tail)
        if len(self._event_lines) > 40:
            self._event_lines = self._event_lines[-40:]

    def _set_total_speed(self, live: list | None) -> None:
        """Velocidad total actual (suma) arriba a la derecha, en verde."""
        if not hasattr(self, "lbl_speed"):
            return
        bps = dlstate.total_live_speed_bps(live or [])
        if bps is None:
            self.lbl_speed.configure(text="")
        else:
            self.lbl_speed.configure(text=dlstate.format_speed_bps(bps))

    def _set_eta(
        self,
        *,
        finished: int,
        total: int,
        live: list | None,
        active: bool,
    ) -> None:
        """Tiempo estimado bajo la barra de progreso."""
        if not hasattr(self, "lbl_eta"):
            return
        if not active:
            self.lbl_eta.configure(text="")
            return
        if getattr(self, "_eta_anchor_progress", None) is None:
            self._eta_anchor_progress = dlstate.batch_progress_fraction(
                finished=finished,
                total=total,
                live_entries=live or [],
            )
            self._batch_started_at = time.time()
            self.lbl_eta.configure(text="")
            return
        secs = dlstate.estimate_batch_eta_seconds(
            finished=finished,
            total=total,
            live_entries=live or [],
            started_at=getattr(self, "_batch_started_at", None),
            baseline_progress=float(self._eta_anchor_progress or 0.0),
        )
        self.lbl_eta.configure(text=dlstate.format_eta_seconds(secs))

    def _reset_eta_clock(self, *, resume: bool = False) -> None:
        """Reinicia el reloj ETA (inicio nuevo o reenganche a cola)."""
        self._batch_started_at = time.time()
        self._eta_anchor_progress = None if resume else 0.0
        if hasattr(self, "lbl_eta"):
            self.lbl_eta.configure(text="")

    def _rebuild_registry(
        self,
        live: list | None,
        *,
        finished: int,
        total: int,
        active: bool,
        summary: str | None = None,
    ) -> None:
        """Reescribe el Text completo (evita el bug de concatenar con tags)."""
        text = dlstate.build_minimal_registry_text(
            finished=finished,
            total=total,
            events=self._event_lines,
            live_entries=live or [],
            summary=summary,
            active=active,
        )
        if self._registry_text == text:
            return
        self._registry_text = text
        self.txt_log.configure(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        if text:
            self.txt_log.insert("1.0", text)
        self.txt_log.see(tk.END)
        self.txt_log.configure(state=tk.DISABLED)

    def _set_progress(self, value: int | float, maximum: int | float = 1) -> None:
        """Actualiza barra + porcentaje (elegante, ancho fijo)."""
        try:
            maximum = float(maximum)
            value = float(value)
        except (TypeError, ValueError):
            maximum, value = 1.0, 0.0
        if maximum <= 0:
            maximum = 1.0
        value = max(0.0, min(value, maximum))
        self.progress.configure(maximum=maximum, value=value)
        pct = int(round(100.0 * value / maximum))
        pct = max(0, min(pct, 100))
        self.progress_pct.configure(text=f"{pct}%")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        # Descargar sigue activo para poder pedir cancelación por error
        self.btn_download.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.txt_urls.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def _is_downloading(self) -> bool:
        if self._busy or (self._worker is not None and self._worker.is_alive()):
            return True
        try:
            return dlstate.active_job_count() > 0
        except Exception:
            return False

    def _request_cancel_download(self) -> bool:
        """Pregunta siempre si hay descarga; si confirma, cancela cola + worker."""
        if not self._is_downloading():
            return False
        if not ask_cancel_download(self):
            return False
        self._stop_flag.set()
        try:
            n = dlstate.cancel_active_jobs()
            dlstate.request_worker_stop()
            self._log(
                f"Cancelación confirmada… ({n} trabajo(s) marcados; "
                "el worker se detendrá)"
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"AVISO al cancelar cola: {exc}")
        return True

    def _on_close(self) -> None:
        active = False
        try:
            active = self._is_downloading() or dlstate.active_job_count() > 0
        except Exception:
            active = self._is_downloading()
        if active:
            answer = messagebox.askyesnocancel(
                "Cerrar",
                "Hay descargas en curso o en cola.\n\n"
                "Sí = cerrar y dejar en SEGUNDO PLANO (sigue descargando)\n"
                "No = CANCELAR descargas y cerrar\n"
                "Cancelar = no cerrar la ventana",
                parent=self,
            )
            if answer is None:
                return
            if answer is False:
                try:
                    dlstate.cancel_active_jobs()
                    dlstate.request_worker_stop()
                except Exception:
                    pass
                self._stop_flag.set()
                self._log("Cierre: descargas canceladas.")
            else:
                self._log("Cierre: descargas siguen en segundo plano.")
        self.destroy()

    def _resume_background_if_needed(self) -> None:
        """Si hay cola/worker activo al abrir, reconecta la UI al estado."""
        try:
            dlstate.init_db()
            active = dlstate.active_job_count()
            running = dlstate.worker_is_running()
        except Exception as exc:  # noqa: BLE001
            self._log(f"AVISO estado cola: {exc}")
            return
        if active <= 0 and not running:
            return
        if active > 0 and not running:
            dlstate.ensure_worker_running()
        self._set_busy(True)
        self._clear_download_log()
        self._reset_eta_clock(resume=True)
        self._start_status_poll()

    def _start_status_poll(self) -> None:
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
        self._poll_status_tick()

    def _poll_status_tick(self) -> None:
        try:
            snap = dlstate.snapshot_status()
            batch_id = self._active_batch_id
            if batch_id:
                prog = dlstate.batch_progress(batch_id)
                total = max(int(prog.get("total", 0)), 1)
                finished = int(prog.get("finished", 0))
                self._set_progress(finished, total)
                counts = prog
                active = (
                    int(prog.get(dlstate.STATUS_PENDING, 0))
                    + int(prog.get(dlstate.STATUS_RUNNING, 0))
                )
            else:
                counts = snap["counts"]
                finished = (
                    counts.get(dlstate.STATUS_DONE, 0)
                    + counts.get(dlstate.STATUS_SKIPPED, 0)
                    + counts.get(dlstate.STATUS_FAILED, 0)
                    + counts.get(dlstate.STATUS_CANCELLED, 0)
                )
                total = max(sum(counts.values()), 1)
                self._set_progress(finished, total)
                active = int(snap.get("active", 0))

            self._last_finished = finished
            self._last_total = total
            self._ingest_worker_events()
            live = snap.get("live_progress") or []
            still_busy = active > 0 or bool(
                self._worker and self._worker.is_alive()
            )

            if not still_busy:
                ok = int(counts.get(dlstate.STATUS_DONE, 0))
                skipped = int(counts.get(dlstate.STATUS_SKIPPED, 0))
                fail = int(counts.get(dlstate.STATUS_FAILED, 0))
                summary = (
                    f"Listo. Descargadas: {ok} | Ya estaban: {skipped} | Fallidos: {fail}"
                )
                self._rebuild_registry(
                    [],
                    finished=finished,
                    total=total,
                    active=False,
                    summary=summary,
                )
                self._set_total_speed([])
                self._set_eta(finished=finished, total=total, live=[], active=False)
                self._set_busy(False)
                self._poll_after_id = None
                self._active_batch_id = None
                if ok or skipped:
                    out = self.download_dir.get()
                    self.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Descarga terminada",
                            f"Descargadas: {ok}\n"
                            f"Ya estaban descargadas: {skipped}\n"
                            f"Fallidos: {fail}\n\n"
                            f"Carpeta:\n{out}",
                        ),
                    )
                return

            self._rebuild_registry(
                live, finished=finished, total=total, active=True
            )
            self._set_total_speed(live)
            self._set_eta(
                finished=finished, total=total, live=live, active=True
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"AVISO poll: {exc}")
        self._poll_after_id = self.after(500, self._poll_status_tick)

    def _start_download(self) -> None:
        if self._is_downloading():
            self._request_cancel_download()
            return

        urls = parse_urls(self.txt_urls.get("1.0", tk.END))
        if not urls:
            messagebox.showwarning(
                "Sin enlaces",
                "Escribe al menos un enlace de YouTube / YouTube Music.",
            )
            return

        out_dir = Path(self.download_dir.get().strip())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Carpeta", f"No se pudo crear la carpeta:\n{exc}")
            return

        self._stop_flag.clear()
        self._set_progress(0, len(urls))
        self._set_busy(True)
        mode = self.format_mode.get()
        if mode in {"mp3", "opus", "webm"} and not self._ffmpeg:
            messagebox.showwarning(
                "Falta ffmpeg",
                "No se encontró ffmpeg.\n\n"
                "Hace falta para MP3 y para Opus (remux + carátula).\n"
                "Instala con: winget install Gyan.FFmpeg",
            )
            self._set_busy(False)
            return
        self._clear_download_log()
        self._sync_cookies_prefs(announce=True)
        self._reset_eta_clock(resume=False)
        self._set_total_speed([])
        self._set_eta(finished=0, total=1, live=[], active=False)

        self._worker = threading.Thread(
            target=self._enqueue_and_watch,
            args=(urls, out_dir, mode),
            daemon=True,
        )
        self._worker.start()

    def _stop_download(self) -> None:
        self._request_cancel_download()

    def _enqueue_and_watch(
        self, urls: list[str], out_dir: Path, format_mode: str
    ) -> None:
        """Encola en SQLite y asegura worker; la UI hace poll del estado."""
        try:
            batch_id = str(uuid.uuid4())
            self._active_batch_id = batch_id
            dlstate.init_db()
            dlstate.create_batch(
                batch_id, "musica", format_mode, str(out_dir), note="canciones"
            )
            dlstate.enqueue_track_jobs(
                batch_id, "musica", urls, str(out_dir), format_mode
            )
            if not dlstate.ensure_worker_running():
                self._log("✗ No se pudo iniciar el worker en segundo plano")
                self.after(0, lambda: self._set_busy(False))
                return
            self.after(0, self._start_status_poll)
        except Exception as exc:  # noqa: BLE001
            self._log(f"✗ Error al encolar: {exc}")
            self.after(0, lambda: self._set_busy(False))

    def _download_one_url(
        self,
        base_cmd: list[str],
        url: str,
        out_dir: Path,
        format_mode: str,
        index: int,
        total: int,
    ) -> tuple[str, tuple[Path, str, dict] | None]:
        """
        Fase 1: descarga audio sin -J previo.
        Metadatos desde --write-info-json; luego mueve a Artista/Álbum/.
        """
        if self._stop_flag.is_set():
            return "fail", None

        self._log(f"\n[{index}/{total}] {url}")
        staging = get_staging_dir(out_dir)
        scrub_delivery_artifacts(out_dir)
        video_id = extract_youtube_id(url)

        try:
            # Skip rápido sin -J si ya conocemos id→nombre de una descarga previa
            hit = cache_lookup_existing(out_dir, video_id)
            if hit:
                basename, existing = hit
                self._link_catalog.add(basename, url)
                self.after(0, lambda: refresh_link_catalog_window(self))
                self._log(f"[{index}] Nombre: {basename}")
                self._log(f"[{index}] ⊘ Ya estaba descargada: {existing.name}")
                return "skipped", None

            cmd = build_download_cmd(
                base_cmd,
                url,
                staging,
                format_mode,
                self._ffmpeg,
                staging=True,
            )
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            assert process.stdout is not None
            for line in process.stdout:
                if self._stop_flag.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    self._log(f"[{index}] Proceso detenido.")
                    break
                line = line.rstrip()
                if line:
                    self._log(f"[{index}] {line}")
            code = process.wait()
            if self._stop_flag.is_set():
                cleanup_staging_id(staging, video_id)
                return "fail", None
            if code != 0:
                cleanup_staging_id(staging, video_id)
                self._log(f"[{index}] ✗ Error (código {code}) en {url}")
                return "fail", None

            info_path = find_info_json(staging, video_id)
            if info_path is None:
                cleanup_staging_id(staging, video_id)
                self._log(f"[{index}] ✗ Sin metadatos (.info.json) tras la descarga")
                return "fail", None

            info = load_info_json_file(info_path)
            if not video_id:
                video_id = str(info.get("id") or "") or None

            basename = song_artist_basename(info)
            artist_folder = folder_artist_from_info(info)
            album_folder = folder_album_from_info(info)
            track_dir = ensure_artist_album_dir(out_dir, artist_folder, album_folder)
            self._link_catalog.add(basename, url)
            self.after(0, lambda: refresh_link_catalog_window(self))
            self._log(f"[{index}] Nombre: {basename}")
            self._log(f"[{index}] Carpeta: {artist_folder} / {album_folder}")

            status, final_path, basename = promote_staged_to_dest(
                staging, track_dir, info
            )
            cache_remember(
                out_dir,
                video_id or str(info.get("id") or "") or None,
                basename,
                artist_folder,
                album_folder,
            )
            if status == "skipped":
                self._log(f"[{index}] ⊘ Ya estaba descargada: {basename}")
                return "skipped", None
            if status == "ok" and final_path is not None:
                self._log(f"[{index}] ✓ Audio listo ({index}/{total})")
                return "ok", (final_path, basename, info)
            self._log(f"[{index}] ✗ No se pudo colocar el archivo final")
            return "fail", None
        except FileNotFoundError:
            self._log(
                "✗ No se encontró yt-dlp. Instálalo con:\n"
                "  python -m pip install yt-dlp"
            )
            self._stop_flag.set()
            return "fail", None
        except Exception as exc:  # noqa: BLE001
            cleanup_staging_id(staging, video_id)
            self._log(f"[{index}] ✗ Excepción: {exc}")
            return "fail", None

    def _apply_extras_batch(
        self, pending: list[tuple[Path, str, dict]], title: str = "Fase 2"
    ) -> None:
        """Fase 2: carátula + letra sobre audios ya descargados."""
        if not pending or self._stop_flag.is_set():
            return
        self._log(f"\n{title}: carátula + letra ({len(pending)} archivo(s))…")

        def _one(item: tuple[Path, str, dict]) -> None:
            if self._stop_flag.is_set():
                return
            audio_path, basename, info = item
            # Re-localizar por si el remux previo de otro hilo no aplica; path es el de fase 1
            self._log(f"  Extras: {basename}")
            try:
                attach_lyrics_and_cover(
                    audio_path,
                    basename,
                    info,
                    self._ffmpeg,
                    self._log,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"  AVISO: extras fallaron en {basename} ({exc})")

        with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as pool:
            futs = [pool.submit(_one, item) for item in pending]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"  AVISO: extras worker ({exc})")

    def _download_all(self, urls: list[str], out_dir: Path, format_mode: str) -> None:
        base_cmd = find_yt_dlp()
        counters = {"ok": 0, "skipped": 0, "fail": 0}
        lock = threading.Lock()
        done = 0
        total = len(urls)
        pending_extras: list[tuple[Path, str, dict]] = []

        self._log(f"Paralelo: hasta {PARALLEL_DOWNLOADS} descargas a la vez")
        self._log("Fase 1: descarga de audio…")

        with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as pool:
            futures = [
                pool.submit(
                    self._download_one_url,
                    base_cmd,
                    url,
                    out_dir,
                    format_mode,
                    index,
                    total,
                )
                for index, url in enumerate(urls, start=1)
            ]
            for fut in as_completed(futures):
                result, extras = fut.result()
                with lock:
                    if result in counters:
                        counters[result] += 1
                    if extras is not None:
                        pending_extras.append(extras)
                    done += 1
                    current = done
                self.after(0, lambda v=current: self._set_progress(v, total))

        if not self._stop_flag.is_set():
            self._apply_extras_batch(pending_extras, title="Fase 2")
        else:
            self._log("Descargas detenidas por el usuario.")

        ok, skipped, fail = counters["ok"], counters["skipped"], counters["fail"]
        self._log(
            f"\nListo. Descargadas: {ok} | Ya estaban: {skipped} | Fallidos: {fail}"
        )
        self.after(0, lambda: self._set_busy(False))
        if (ok or skipped) and not self._stop_flag.is_set():
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Descarga terminada",
                    f"Descargadas: {ok}\n"
                    f"Ya estaban descargadas: {skipped}\n"
                    f"Fallidos: {fail}\n\n"
                    f"Carpeta:\n{out_dir}",
                ),
            )


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
