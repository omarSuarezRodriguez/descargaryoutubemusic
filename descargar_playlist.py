"""
Descargador de playlists / álbumes desde YouTube Music / YouTube.
Clon funcional de descargar_musica.py orientado a playlists:
cada playlist se guarda en CarpetaBase / Artista - Álbum (año) /.

NO modifica descargar_musica.py.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from descargar_musica import (
    build_download_cmd,
    clean_filename,
    fetch_video_info,
    find_existing_download,
    find_ffmpeg,
    find_yt_dlp,
    parse_urls,
    song_artist_basename,
)
from metadata_extras import attach_lyrics_and_cover

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
    """Nombre de álbum/playlist limpio (sin artista duplicado)."""
    album = _as_text(playlist_info.get("album"))
    title = (
        playlist_info.get("title") or playlist_info.get("playlist_title") or ""
    ).strip()
    album_name = album or title or "playlist"
    if not artist:
        artist = playlist_artist_name(playlist_info)
    return strip_artist_from_album(album_name, artist)


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


def playlist_folder_name(
    playlist_info: dict,
    log=None,
    base_cmd: list[str] | None = None,
) -> str:
    """
    Nombre de carpeta: 'Artista - Álbum (YYYY)'
    Si no hay año: 'Artista - Álbum'
    Enriquece artista/álbum desde entradas o la primera pista si el flat JSON viene pobre.
    """
    artist, track_info = resolve_playlist_artist(
        playlist_info, base_cmd=base_cmd, log=log
    )

    album_name = playlist_album_name(playlist_info, artist=artist)
    if isinstance(track_info, dict):
        track_album = _as_text(track_info.get("album"))
        if track_album:
            album_name = strip_artist_from_album(track_album, artist)
            if log:
                log(f"Álbum (pista completa): {album_name}")

    year = resolve_album_year(
        playlist_info,
        artist,
        album_name,
        log=log,
        track_info=track_info,
    )

    if artist and album_name:
        base = f"{artist} - {album_name}"
    elif album_name:
        base = album_name
    elif artist:
        base = artist
    else:
        base = "playlist"

    if year:
        base = f"{base} ({year})"
    return clean_filename(base)


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

        self._build_ui()
        self.after(100, self._drain_log_queue)
        self.after(200, self._check_deps)
        self.after(400, self._poll_clipboard)

    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            root,
            text="Enlaces de playlist/álbum (uno por línea). Carpeta: Artista - Álbum (año)/:",
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
        self.progress = ttk.Progressbar(btn_row, mode="determinate")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))

        ttk.Label(root, text="Registro:").pack(anchor=tk.W, padx=12, pady=(8, 0))
        self.txt_log = scrolledtext.ScrolledText(
            root, height=10, wrap=tk.WORD, state=tk.DISABLED
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, **pad)

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

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_download.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if busy else tk.DISABLED)
        self.txt_urls.configure(state=tk.DISABLED if busy else tk.NORMAL)

    def _start_download(self) -> None:
        if self._worker and self._worker.is_alive():
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
        self.progress.configure(maximum=max(len(urls), 1), value=0)
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
        self._log(f"Carpeta base: {out_dir}")
        self._log("Estructura: CarpetaBase / Artista - Álbum (año) / canción - artista")
        self._log("Extras: carátula + letra embebidas (1 solo archivo por pista)")

        self._worker = threading.Thread(
            target=self._download_all_playlists,
            args=(urls, out_dir, mode),
            daemon=True,
        )
        self._worker.start()

    def _stop_download(self) -> None:
        self._stop_flag.set()
        self._log("Detención solicitada… (terminará el archivo actual)")

    def _download_one_track(
        self,
        base_cmd: list[str],
        track_url: str,
        album_dir: Path,
        format_mode: str,
        counters: dict[str, int],
    ) -> None:
        info = fetch_video_info(base_cmd, track_url)
        basename = song_artist_basename(info)
        self._log(f"  Nombre: {basename}")

        existing = find_existing_download(album_dir, basename)
        if existing:
            counters["skipped"] += 1
            self._log(f"  ⊘ Ya estaba: {existing.name}")
            return

        cmd = build_download_cmd(
            base_cmd, track_url, album_dir, format_mode, self._ffmpeg, basename
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
                self._log("  Proceso actual detenido.")
                break
            line = line.rstrip()
            if line:
                self._log(line)
        code = process.wait()
        if self._stop_flag.is_set():
            counters["fail"] += 1
            return
        if code == 0:
            counters["ok"] += 1
            self._log("  ✓ Completado")
            audio_path = find_existing_download(album_dir, basename)
            if audio_path:
                attach_lyrics_and_cover(
                    audio_path,
                    basename,
                    info,
                    self._ffmpeg,
                    self._log,
                )
            else:
                self._log("  AVISO: audio OK pero no se encontró el archivo para extras")
        else:
            counters["fail"] += 1
            self._log(f"  ✗ Error (código {code})")

    def _download_all_playlists(
        self, playlist_urls: list[str], base_dir: Path, format_mode: str
    ) -> None:
        base_cmd = find_yt_dlp()
        counters = {"ok": 0, "skipped": 0, "fail": 0}
        # Primero resolver playlists para progress total
        resolved: list[tuple[str, dict, list[dict], Path]] = []
        total_tracks = 0

        for p_index, purl in enumerate(playlist_urls, start=1):
            if self._stop_flag.is_set():
                break
            self._log(f"\n=== Playlist {p_index}/{len(playlist_urls)} ===")
            self._log(purl)
            try:
                plist = fetch_playlist_info(base_cmd, purl)
                entries = playlist_entries(plist)
                if not entries and plist.get("id") and plist.get("_type") != "playlist":
                    # URL de un solo vídeo: tratar como “álbum” de 1 pista
                    entries = [plist]
                folder = playlist_folder_name(
                    plist, log=self._log, base_cmd=base_cmd
                ) or clean_filename(song_artist_basename(plist))
                if not entries:
                    counters["fail"] += 1
                    self._log("✗ Playlist vacía o no se pudieron listar pistas")
                    continue
                album_dir = base_dir / folder
                album_dir.mkdir(parents=True, exist_ok=True)
                self._log(f"Álbum/carpeta: {folder} ({len(entries)} pista(s))")
                self._log(f"Destino: {album_dir}")
                resolved.append((purl, plist, entries, album_dir))
                total_tracks += len(entries)
            except FileNotFoundError:
                counters["fail"] += 1
                self._log(
                    "✗ No se encontró yt-dlp. Instálalo con:\n"
                    "  python -m pip install yt-dlp"
                )
                self.after(0, lambda: self._set_busy(False))
                return
            except Exception as exc:  # noqa: BLE001
                counters["fail"] += 1
                self._log(f"✗ No se pudo leer la playlist: {exc}")

        if total_tracks <= 0:
            self._log("\nNo hay pistas para descargar.")
            self.after(0, lambda: self._set_busy(False))
            return

        self.after(0, lambda: self.progress.configure(maximum=total_tracks, value=0))
        done = 0

        for purl, _plist, entries, album_dir in resolved:
            if self._stop_flag.is_set():
                self._log("Descargas detenidas por el usuario.")
                break
            self._log(f"\n--- Descargando en: {album_dir.name} ---")
            for t_index, entry in enumerate(entries, start=1):
                if self._stop_flag.is_set():
                    self._log("Descargas detenidas por el usuario.")
                    break
                track_url = entry_watch_url(entry)
                if not track_url:
                    counters["fail"] += 1
                    done += 1
                    self.after(0, lambda v=done: self.progress.configure(value=v))
                    continue
                self._log(f"\n[{t_index}/{len(entries)}] {track_url}")
                try:
                    self._download_one_track(
                        base_cmd, track_url, album_dir, format_mode, counters
                    )
                except FileNotFoundError:
                    counters["fail"] += 1
                    self._log("✗ No se encontró yt-dlp.")
                    self.after(0, lambda: self._set_busy(False))
                    return
                except Exception as exc:  # noqa: BLE001
                    counters["fail"] += 1
                    self._log(f"✗ Excepción: {exc}")
                done += 1
                self.after(0, lambda v=done: self.progress.configure(value=v))

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
                    f"Carpeta base:\n{base_dir}",
                ),
            )


def main() -> None:
    app = PlaylistApp()
    app.mainloop()


if __name__ == "__main__":
    main()
