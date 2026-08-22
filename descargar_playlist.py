"""
Descargador de playlists / álbumes desde YouTube Music / YouTube.
Clon funcional de descargar_musica.py orientado a playlists:
cada playlist se guarda en CarpetaBase / Artista / NombreAlbum /.

NO modifica descargar_musica.py (solo importa helpers).
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import tkinter as tk
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import download_state as dlstate
from descargar_musica import (
    PARALLEL_DOWNLOADS,
    ask_cancel_download,
    build_download_cmd,
    cache_lookup_existing,
    cache_remember,
    clean_filename,
    cleanup_staging_id,
    ensure_artist_album_dir,
    extract_youtube_id,
    fetch_video_info,
    find_existing_download,
    find_ffmpeg,
    find_info_json,
    find_yt_dlp,
    load_info_json_file,
    normalize_album_folder_name,
    parse_urls,
    promote_staged_to_dest,
    song_artist_basename,
)
from metadata_extras import attach_lyrics_and_cover
from link_catalog import LinkCatalog, refresh_link_catalog_window, show_link_catalog_window

# Uploaders/canales que NO son el artista del álbum
_INVALID_ARTISTS = {
    "",
    "youtube",
    "youtube music",
    "youtubemusic",
    "various artists",
    "varios artistas",
    "varios",
    "unknown artist",
    "unknown",
    "topic",
}


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_as_text(v) for v in value if v]
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        return _as_text(value.get("name") or value.get("text") or "")
    return str(value).strip()


def _clean_artist(raw: object) -> str:
    """Normaliza artista y descarta valores inútiles de YouTube."""
    artist = _as_text(raw)
    if artist.casefold().endswith(" - topic"):
        artist = artist[: -len(" - Topic")].strip()
    if artist.casefold() in _INVALID_ARTISTS:
        return ""
    return artist


def artist_from_info(info: dict) -> str:
    """Extrae artista usable de metadatos de playlist o pista."""
    if not isinstance(info, dict):
        return ""
    for key in (
        "artist",
        "album_artist",
        "creator",
        "artists",
        "uploader",
        "channel",
        "playlist_uploader",
        "playlist_channel",
    ):
        artist = _clean_artist(info.get(key))
        if artist:
            return artist
    return ""


def artist_from_title(title: str) -> str:
    """Último recurso: 'Artista - Canción' en el título."""
    title = (title or "").strip()
    if " - " not in title:
        return ""
    left, _right = title.split(" - ", 1)
    return _clean_artist(left)


def playlist_artist_name(playlist_info: dict) -> str:
    """Artista a nivel playlist (puede estar vacío con --flat-playlist)."""
    return artist_from_info(playlist_info)


def strip_artist_from_album(album_name: str, artist: str) -> str:
    """Quita 'Artista - ' / 'Artista ' del inicio del álbum si viene duplicado."""
    album_name = (album_name or "").strip() or "playlist"
    artist = (artist or "").strip()
    if not artist:
        return album_name
    dash_prefix = f"{artist} - "
    if album_name.casefold().startswith(dash_prefix.casefold()):
        return album_name[len(dash_prefix) :].strip() or album_name
    prefix = f"{artist} "
    if album_name.casefold().startswith(prefix.casefold()):
        rest = album_name[len(prefix) :].strip()
        return rest or album_name
    return album_name


def playlist_album_name(playlist_info: dict, artist: str = "") -> str:
    """Nombre de álbum/playlist limpio (sin artista duplicado ni prefijo 'Album -')."""
    album = _as_text(playlist_info.get("album"))
    title = (
        playlist_info.get("title") or playlist_info.get("playlist_title") or ""
    ).strip()
    album_name = album or title or "playlist"
    if not artist:
        artist = playlist_artist_name(playlist_info)
    album_name = strip_artist_from_album(album_name, artist)
    return normalize_album_folder_name(album_name)


def resolve_playlist_artist(
    playlist_info: dict,
    base_cmd: list[str] | None = None,
    log=None,
) -> tuple[str, dict | None]:
    """
    Cascada de artista: playlist -> entradas flat -> primera pista completa -> título.
    Devuelve (artista, info_pista_completa|None).
    """
    artist = playlist_artist_name(playlist_info)
    if artist:
        if log:
            log(f"Artista (playlist): {artist}")
        return artist, None

    entries = playlist_info.get("entries") or []
    for entry in entries[:8]:
        if not isinstance(entry, dict):
            continue
        artist = artist_from_info(entry)
        if artist:
            if log:
                log(f"Artista (entrada): {artist}")
            return artist, None

    track_info: dict | None = None
    if base_cmd:
        for entry in entries[:3]:
            if not isinstance(entry, dict):
                continue
            url = entry_watch_url(entry)
            if not url:
                continue
            try:
                if log:
                    log("Buscando artista en metadatos de la primera pista…")
                track_info = fetch_video_info(base_cmd, url)
            except Exception as exc:  # noqa: BLE001
                if log:
                    log(f"AVISO: no se pudieron leer metadatos de pista: {exc}")
                track_info = None
                continue
            artist = artist_from_info(track_info)
            if artist:
                if log:
                    log(f"Artista (pista completa): {artist}")
                return artist, track_info
            # Título de la pista completa (mejor que flat)
            artist = artist_from_title(_as_text(track_info.get("title")))
            if artist:
                if log:
                    log(f"Artista (título pista): {artist}")
                return artist, track_info

    for entry in entries[:8]:
        if not isinstance(entry, dict):
            continue
        artist = artist_from_title(_as_text(entry.get("title")))
        if artist:
            if log:
                log(f"Artista (título entrada): {artist}")
            return artist, track_info

    if log:
        log("AVISO: no se encontró artista para la carpeta")
    return "", track_info


def _year_from_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # YYYY
    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        if 1900 <= year <= 2100:
            return text
    # YYYYMMDD (yt-dlp upload_date / release_date)
    if re.fullmatch(r"\d{8}", text):
        year = int(text[:4])
        if 1900 <= year <= 2100:
            return text[:4]
    # ISO date 1973-03-01...
    m = re.match(r"^(\d{4})[-/]", text)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return m.group(1)
    return None


def year_from_youtube_info(info: dict) -> str | None:
    """Año de álbum desde metadatos YT si es fiable (no upload_date)."""
    for key in ("release_year", "album_release_year"):
        y = _year_from_text(info.get(key))
        if y:
            return y
    for key in ("release_date", "album_release_date"):
        y = _year_from_text(info.get(key))
        if y:
            return y
    return None


def fetch_itunes_album_year(artist: str, album: str) -> str | None:
    """Busca el año del álbum en iTunes Search API."""
    import urllib.parse
    import urllib.request

    if not artist or not album:
        return None
    term = f"{artist} {album}".strip()
    query = urllib.parse.urlencode(
        {"term": term, "entity": "album", "limit": 15, "media": "music"}
    )
    url = f"https://itunes.apple.com/search?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "descargaryoutubemusic-playlist/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    results = data.get("results") or []
    artist_cf = artist.casefold()
    album_cf = album.casefold()

    candidates: list[tuple[int, str]] = []
    for item in results:
        if not isinstance(item, dict) or not item.get("releaseDate"):
            continue
        a_name = str(item.get("artistName") or "").casefold()
        c_name = str(item.get("collectionName") or "").casefold()
        s = 0
        if artist_cf and artist_cf == a_name:
            s += 4
        elif artist_cf and artist_cf in a_name:
            s += 2
        if album_cf and c_name == album_cf:
            s += 5
        elif album_cf and album_cf in c_name:
            # Evitar "Album Live", "Album Remastered" si hay exacto luego
            s += 3
        if s < 6:
            continue
        y = _year_from_text(item.get("releaseDate"))
        if y:
            candidates.append((s, y))

    if not candidates:
        return None
    # Mejor score; a igualdad, año más antiguo (edición original)
    candidates.sort(key=lambda t: (-t[0], t[1]))
    best_score = candidates[0][0]
    years = [y for s, y in candidates if s == best_score]
    return min(years)


def fetch_musicbrainz_album_year(artist: str, album: str) -> str | None:
    """Fallback fiable: MusicBrainz release-group first-release-date."""
    import time
    import urllib.error
    import urllib.parse
    import urllib.request

    if not artist or not album:
        return None
    a = artist.replace('"', "")
    al = album.replace('"', "")
    lucene = f'releasegroup:"{al}" AND artist:"{a}"'
    query = urllib.parse.urlencode({"query": lucene, "fmt": "json", "limit": "5"})
    url = f"https://musicbrainz.org/ws/2/release-group?{query}"

    data = None
    for attempt in range(3):
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "descargaryoutubemusic/1.0 (local playlist tool)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            break
        except urllib.error.HTTPError as exc:
            # 503 = rate limit típico de MusicBrainz
            if exc.code in {503, 429} and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            return None

    if not isinstance(data, dict):
        return None
    groups = data.get("release-groups") or []
    album_cf = album.casefold()
    exact_years: list[str] = []
    partial_years: list[str] = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        title = str(g.get("title") or "").casefold()
        y = _year_from_text(g.get("first-release-date"))
        if not y:
            continue
        if title == album_cf:
            exact_years.append(y)
        elif album_cf in title or title in album_cf:
            partial_years.append(y)
    if exact_years:
        return min(exact_years)
    if partial_years:
        return min(partial_years)
    for g in groups:
        if not isinstance(g, dict):
            continue
        y = _year_from_text(g.get("first-release-date"))
        if y:
            return y
    return None


def resolve_album_year(
    playlist_info: dict,
    artist: str,
    album: str,
    log=None,
    track_info: dict | None = None,
) -> str | None:
    """YouTube (playlist/entradas) -> iTunes -> MusicBrainz -> pista completa."""
    y = year_from_youtube_info(playlist_info)
    if y:
        if log:
            log(f"Año (YouTube): {y}")
        return y
    entries = playlist_info.get("entries") or []
    for entry in entries[:3]:
        if isinstance(entry, dict):
            y = year_from_youtube_info(entry)
            if y:
                if log:
                    log(f"Año (YouTube pista): {y}")
                return y
    y = fetch_itunes_album_year(artist, album)
    if y:
        if log:
            log(f"Año (iTunes): {y}")
        return y
    y = fetch_musicbrainz_album_year(artist, album)
    if y:
        if log:
            log(f"Año (MusicBrainz): {y}")
        return y
    # Último recurso: release de una pista (puede ser remaster)
    if isinstance(track_info, dict):
        y = year_from_youtube_info(track_info)
        if y:
            if log:
                log(f"Año (YouTube pista completa): {y}")
            return y
    if log:
        log("AVISO: no se encontró año del álbum")
    return None


def playlist_artist_album(
    playlist_info: dict,
    log=None,
    base_cmd: list[str] | None = None,
) -> tuple[str, str]:
    """
    Artista y NombreAlbum para carpeta:
    base / Artista / NombreAlbum /
    Enriquece desde entradas o primera pista si el flat JSON viene pobre.
    (Sin año en la ruta — más rápido al iniciar cada álbum.)
    """
    artist, track_info = resolve_playlist_artist(
        playlist_info, base_cmd=base_cmd, log=log
    )

    album_name = playlist_album_name(playlist_info, artist=artist)
    if isinstance(track_info, dict):
        track_album = _as_text(track_info.get("album"))
        if track_album:
            album_name = normalize_album_folder_name(
                strip_artist_from_album(track_album, artist)
            )
            if log:
                log(f"Álbum (pista completa): {album_name}")

    artist_clean = clean_filename(artist) if artist else ""
    album_clean = (
        clean_filename(normalize_album_folder_name(album_name)) if album_name else ""
    )
    if not artist_clean:
        artist_clean = "Artista desconocido"
    if not album_clean:
        album_clean = "Sin álbum"
    return artist_clean, album_clean


def playlist_folder_name(
    playlist_info: dict,
    log=None,
    base_cmd: list[str] | None = None,
) -> str:
    """Compat: ruta relativa 'Artista/NombreAlbum'."""
    artist, album = playlist_artist_album(
        playlist_info, log=log, base_cmd=base_cmd
    )
    return f"{artist}/{album}"


def entry_watch_url(entry: dict) -> str | None:
    """URL de una pista dentro de la playlist."""
    if not isinstance(entry, dict):
        return None
    if entry.get("_type") == "playlist":
        return None
    url = entry.get("url") or entry.get("webpage_url")
    if isinstance(url, str) and url.startswith("http"):
        # Preferir watch limpio si hay id
        vid = entry.get("id")
        if vid and re.fullmatch(r"[\w-]{6,}", str(vid)):
            return f"https://www.youtube.com/watch?v={vid}"
        return url
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def fetch_playlist_info(base_cmd: list[str], url: str) -> dict:
    """Obtiene metadatos de playlist y entradas (flat) sin descargar."""
    cmd = [
        *base_cmd,
        "--skip-download",
        "--flat-playlist",
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
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("Respuesta de playlist inválida")
    return data


def playlist_entries(playlist_info: dict) -> list[dict]:
    entries = playlist_info.get("entries") or []
    out: list[dict] = []
    for entry in entries:
        if isinstance(entry, dict) and entry_watch_url(entry):
            out.append(entry)
    return out


class PlaylistApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Descargar Playlists YouTube Music")
        self.geometry("720x560")
        self.minsize(560, 420)

        self.download_dir = tk.StringVar(
            value=str(Path.home() / "Downloads" / "YouTubeMusic" / "Playlists")
        )
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._ffmpeg = find_ffmpeg()
        self._busy = False
        self._last_clip: str | None = None
        self.clipboard_watch = tk.BooleanVar(value=True)
        self.format_mode = tk.StringVar(value="mp3")
        self._link_catalog = LinkCatalog()
        self._active_batch_id: str | None = None
        self._poll_after_id: str | None = None
        self._log_offset = 0

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_log_queue)
        self.after(200, self._check_deps)
        self.after(400, self._poll_clipboard)
        self.after(600, self._resume_background_if_needed)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            root,
            text="Enlaces de playlist/álbum (uno por línea). Carpeta: Artista / NombreAlbum /:",
        ).pack(anchor=tk.W, **pad)

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
        ttk.Label(folder_row, text="Carpeta base:").pack(side=tk.LEFT)
        ttk.Entry(folder_row, textvariable=self.download_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        ttk.Button(folder_row, text="Elegir…", command=self._choose_folder).pack(
            side=tk.LEFT
        )

        btn_row = ttk.Frame(root)
        btn_row.pack(fill=tk.X, **pad)
        self.btn_download = ttk.Button(
            btn_row, text="Descargar playlists", command=self._start_download
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

    def _read_clipboard(self) -> str:
        try:
            return self.clipboard_get().strip()
        except tk.TclError:
            return ""

    def append_urls_from_text(self, text: str) -> list[str]:
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
                self._last_clip = clip
            elif not self.clipboard_watch.get() or self._busy:
                self._last_clip = clip
            elif clip and clip != self._last_clip:
                self._last_clip = clip
                added = self.append_urls_from_text(clip)
                for url in added:
                    self._log(f"Portapapeles -> {url}")
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

    def _drain_log_queue(self) -> None:
        while True:
            try:
                message = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.txt_log.configure(state=tk.NORMAL)
            self.txt_log.insert(tk.END, message + "\n")
            self.txt_log.see(tk.END)
            self.txt_log.configure(state=tk.DISABLED)
        self.after(100, self._drain_log_queue)

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
        if not self._is_downloading():
            return False
        if not ask_cancel_download(self):
            return False
        self._stop_flag.set()
        try:
            n = dlstate.cancel_active_jobs()
            dlstate.request_worker_stop()
            self._log(
                f"Cancelación confirmada… ({n} trabajo(s); worker se detendrá)"
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
        self._log(
            f"Estado recuperado: {active} en cola/ejecución. "
            "Las descargas continúan en segundo plano."
        )
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

            tail = snap.get("log_tail") or []
            if len(tail) > self._log_offset:
                for line in tail[self._log_offset :]:
                    self._log(line)
                self._log_offset = len(tail)

            if active <= 0 and not (self._worker and self._worker.is_alive()):
                ok = int(counts.get(dlstate.STATUS_DONE, 0))
                skipped = int(counts.get(dlstate.STATUS_SKIPPED, 0))
                fail = int(counts.get(dlstate.STATUS_FAILED, 0))
                self._log(
                    f"\nListo. Descargadas: {ok} | Ya estaban: {skipped} | Fallidos: {fail}"
                )
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
                            f"Carpeta base:\n{out}",
                        ),
                    )
                return
        except Exception as exc:  # noqa: BLE001
            self._log(f"AVISO poll: {exc}")
        self._poll_after_id = self.after(800, self._poll_status_tick)

    def _start_download(self) -> None:
        if self._is_downloading():
            self._request_cancel_download()
            return

        urls = parse_urls(self.txt_urls.get("1.0", tk.END))
        if not urls:
            messagebox.showwarning(
                "Sin enlaces",
                "Escribe al menos un enlace de playlist / álbum de YouTube Music.",
            )
            return

        out_dir = Path(self.download_dir.get().strip())
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Carpeta", f"No se pudo crear la carpeta:\n{exc}")
            return

        self._stop_flag.clear()
        self._set_progress(0, max(len(urls), 1))
        self._set_busy(True)
        mode = self.format_mode.get()
        mode_label = (
            "MP3 (compatible)"
            if mode == "mp3"
            else "Opus (tal cual YouTube, máxima fidelidad)"
        )
        if mode in {"mp3", "opus", "webm"} and not self._ffmpeg:
            messagebox.showwarning(
                "Falta ffmpeg",
                "No se encontró ffmpeg.\n\n"
                "Hace falta para MP3 y para Opus (remux + carátula).\n"
                "Instala con: winget install Gyan.FFmpeg",
            )
            self._set_busy(False)
            return

        self._log(f"Iniciando {len(urls)} playlist(s)…")
        self._log(f"Formato: {mode_label}")
        self._log(f"Paralelo: hasta {PARALLEL_DOWNLOADS} a la vez (worker)")
        self._log(f"Carpeta base: {out_dir}")
        self._log("Estado: cola durable + worker en segundo plano")
        self._log("Estructura: Artista / NombreAlbum / canción - artista")

        self._worker = threading.Thread(
            target=self._download_all_playlists,
            args=(urls, out_dir, mode),
            daemon=True,
        )
        self._worker.start()

    def _stop_download(self) -> None:
        self._request_cancel_download()

    def _download_one_track(
        self,
        base_cmd: list[str],
        track_url: str,
        album_dir: Path,
        format_mode: str,
        counters: dict[str, int],
        counters_lock: threading.Lock,
        label: str = "",
    ) -> tuple[Path, str, dict] | None:
        """
        Fase 1: audio sin -J previo (metadatos vía --write-info-json).
        """
        prefix = f"{label} " if label else "  "
        if self._stop_flag.is_set():
            with counters_lock:
                counters["fail"] += 1
            return None

        self._log(f"\n{prefix}{track_url}")
        staging = album_dir / ".staging"
        staging.mkdir(parents=True, exist_ok=True)
        video_id = extract_youtube_id(track_url)
        # Caché en carpeta base (padre del artista) o en album_dir: usamos album_dir.parent.parent
        # si estructura base/Artista/Album; más simple: cache en album_dir
        cache_base = album_dir

        try:
            hit = cache_lookup_existing(
                cache_base, video_id, fixed_dest=album_dir
            )
            if hit:
                basename, existing = hit
                self._link_catalog.add(basename, track_url)
                self.after(0, lambda: refresh_link_catalog_window(self))
                self._log(f"{prefix}Nombre: {basename}")
                with counters_lock:
                    counters["skipped"] += 1
                self._log(f"{prefix}⊘ Ya estaba: {existing.name}")
                return None

            cmd = build_download_cmd(
                base_cmd,
                track_url,
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
                    self._log(f"{prefix}Proceso detenido.")
                    break
                line = line.rstrip()
                if line:
                    self._log(f"{prefix}{line}")
            code = process.wait()
            if self._stop_flag.is_set():
                cleanup_staging_id(staging, video_id)
                with counters_lock:
                    counters["fail"] += 1
                return None
            if code != 0:
                cleanup_staging_id(staging, video_id)
                with counters_lock:
                    counters["fail"] += 1
                self._log(f"{prefix}✗ Error (código {code})")
                return None

            info_path = find_info_json(staging, video_id)
            if info_path is None:
                cleanup_staging_id(staging, video_id)
                with counters_lock:
                    counters["fail"] += 1
                self._log(f"{prefix}✗ Sin metadatos (.info.json) tras la descarga")
                return None

            info = load_info_json_file(info_path)
            if not video_id:
                video_id = str(info.get("id") or "") or None

            basename = song_artist_basename(info)
            self._link_catalog.add(basename, track_url)
            self.after(0, lambda: refresh_link_catalog_window(self))
            self._log(f"{prefix}Nombre: {basename}")

            status, final_path, basename = promote_staged_to_dest(
                staging, album_dir, info
            )
            artist_name = album_dir.parent.name if album_dir.parent else "Artista"
            album_name = album_dir.name
            cache_remember(
                cache_base,
                video_id or str(info.get("id") or "") or None,
                basename,
                artist_name,
                album_name,
            )
            if status == "skipped":
                with counters_lock:
                    counters["skipped"] += 1
                self._log(f"{prefix}⊘ Ya estaba: {basename}")
                return None
            if status == "ok" and final_path is not None:
                with counters_lock:
                    counters["ok"] += 1
                self._log(f"{prefix}✓ Audio listo")
                return final_path, basename, info

            with counters_lock:
                counters["fail"] += 1
            self._log(f"{prefix}✗ No se pudo colocar el archivo final")
            return None
        except FileNotFoundError:
            with counters_lock:
                counters["fail"] += 1
            self._log("✗ No se encontró yt-dlp.")
            self._stop_flag.set()
            return None
        except Exception as exc:  # noqa: BLE001
            cleanup_staging_id(staging, video_id)
            with counters_lock:
                counters["fail"] += 1
            self._log(f"{prefix}✗ Excepción: {exc}")
            return None

    def _apply_extras_batch(
        self, pending: list[tuple[Path, str, dict]], title: str = "Fase 2"
    ) -> None:
        if not pending or self._stop_flag.is_set():
            return
        self._log(f"\n{title}: carátula + letra ({len(pending)} archivo(s))…")

        def _one(item: tuple[Path, str, dict]) -> None:
            if self._stop_flag.is_set():
                return
            audio_path, basename, info = item
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

    def _download_all_playlists(
        self, playlist_urls: list[str], base_dir: Path, format_mode: str
    ) -> None:
        """
        Resuelve playlists una a una (crea Artista/Álbum), encola pistas en SQLite
        y deja el worker en segundo plano descargar (persiste al cerrar la UI).
        """
        base_cmd = find_yt_dlp()
        batch_id = str(uuid.uuid4())
        self._active_batch_id = batch_id
        track_urls: list[str] = []
        album_dirs: list[str | None] = []

        try:
            dlstate.init_db()
            dlstate.create_batch(
                batch_id, "playlist", format_mode, str(base_dir), note="playlists"
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"✗ No se pudo preparar la cola: {exc}")
            self.after(0, lambda: self._set_busy(False))
            return

        self._log("Carpetas: se crean una a una al preparar cada álbum")
        self._log("Descarga: worker en segundo plano (cola durable)")

        for p_index, purl in enumerate(playlist_urls, start=1):
            if self._stop_flag.is_set():
                self._log("Preparación detenida por el usuario.")
                break

            self._log(f"\n=== Playlist {p_index}/{len(playlist_urls)} ===")
            self._log(purl)
            try:
                plist = fetch_playlist_info(base_cmd, purl)
                entries = playlist_entries(plist)
                if not entries and plist.get("id") and plist.get("_type") != "playlist":
                    entries = [plist]
                if not entries:
                    self._log("✗ Playlist vacía o no se pudieron listar pistas")
                    continue

                artist_name, album_name = playlist_artist_album(
                    plist, log=self._log, base_cmd=base_cmd
                )
                album_dir = ensure_artist_album_dir(base_dir, artist_name, album_name)
                self._log(
                    f"Álbum/carpeta: {artist_name} / {album_name} "
                    f"({len(entries)} pista(s))"
                )
                self._log(f"Destino: {album_dir}")

                added = 0
                for entry in entries:
                    track_url = entry_watch_url(entry)
                    if not track_url:
                        continue
                    track_urls.append(track_url)
                    album_dirs.append(str(album_dir))
                    added += 1
                self._log(f"Encolando {added} pista(s) de este álbum…")

            except FileNotFoundError:
                self._log(
                    "✗ No se encontró yt-dlp. Instálalo con:\n"
                    "  python -m pip install yt-dlp"
                )
                self.after(0, lambda: self._set_busy(False))
                return
            except Exception as exc:  # noqa: BLE001
                self._log(f"✗ No se pudo leer la playlist: {exc}")

        if not track_urls:
            self._log("\nNo hay pistas para encolar.")
            self.after(0, lambda: self._set_busy(False))
            return

        try:
            n = dlstate.enqueue_track_jobs(
                batch_id,
                "playlist",
                track_urls,
                str(base_dir),
                format_mode,
                album_dirs=album_dirs,
            )
            self._log(f"Total encolado: {n} (batch {batch_id[:8]}…)")
            if not dlstate.ensure_worker_running():
                self._log("✗ No se pudo iniciar el worker en segundo plano")
                self.after(0, lambda: self._set_busy(False))
                return
            self._log("Worker activo: puedes cerrar la UI y seguirá descargando")
            self._log_offset = len(dlstate.read_worker_log_tail(10_000))
            self.after(0, self._start_status_poll)
        except Exception as exc:  # noqa: BLE001
            self._log(f"✗ Error al encolar: {exc}")
            self.after(0, lambda: self._set_busy(False))


def main() -> None:
    app = PlaylistApp()
    app.mainloop()


if __name__ == "__main__":
    main()
