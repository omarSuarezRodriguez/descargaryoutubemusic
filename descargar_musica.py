"""
Descargador de audio desde YouTube Music / YouTube.
Pega varios enlaces (uno por línea) y descarga MP3 u Opus.
Hasta 2 descargas en paralelo (misma calidad; solo velocidad).
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from metadata_extras import attach_lyrics_and_cover
from link_catalog import LinkCatalog, refresh_link_catalog_window, show_link_catalog_window

AUDIO_EXTENSIONS = {".mp3", ".webm", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
# Solo velocidad: 2 pistas a la vez (no afecta calidad/formato)
PARALLEL_DOWNLOADS = 2


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
    album = str(album).strip()
    if not album:
        return "Sin álbum"
    return clean_filename(album) or "Sin álbum"


def ensure_artist_album_dir(base_dir: Path, artist: str, album: str) -> Path:
    """
    Destino: base / Artista / NombreAlbum /
    Si existe, reutiliza; si no, crea (parents=True, exist_ok=True).
    """
    artist_dir = clean_filename(artist) or "Artista desconocido"
    album_dir = clean_filename(album) or "Sin álbum"
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
    """Obtiene metadatos sin descargar."""
    cmd = [
        *base_cmd,
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


def build_download_cmd(
    base_cmd: list[str],
    url: str,
    out_dir: Path,
    format_mode: str,
    ffmpeg: Path | None,
    basename: str,
) -> list[str]:
    """Arma el comando yt-dlp según MP3 (convertir) u Opus/WebM (tal cual)."""
    cmd = [
        *base_cmd,
        "-f",
        "bestaudio",
        # Máximo throughput razonable por archivo (fragments concurrentes)
        "-N",
        "16",
        "-o",
        str(out_dir / f"{basename}.%(ext)s"),
        "--no-playlist",
    ]
    # opus/webm: audio original de YouTube (luego se remuxa a .opus para carátula)
    if format_mode in {"opus", "webm"}:
        pass
    else:
        # MP3 máxima calidad VBR
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
        self._link_catalog = LinkCatalog()

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
            text="Enlaces (uno por línea). Si copias un enlace, se añade solo:",
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
        ttk.Label(folder_row, text="Carpeta:").pack(side=tk.LEFT)
        ttk.Entry(folder_row, textvariable=self.download_dir).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8)
        )
        ttk.Button(folder_row, text="Elegir…", command=self._choose_folder).pack(
            side=tk.LEFT
        )

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
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))

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
        self.progress.configure(maximum=len(urls), value=0)
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
        self._log(f"Iniciando descarga de {len(urls)} enlace(s)…")
        self._log(f"Formato: {mode_label}")
        self._log(f"Paralelo: hasta {PARALLEL_DOWNLOADS} a la vez")
        self._log(f"Carpeta: {out_dir}")
        self._log("Estructura: Artista / NombreAlbum / canción - artista")
        self._log("Nombre: canción - artista")
        self._log("Extras: carátula + letra embebidas (1 solo archivo)")

        self._worker = threading.Thread(
            target=self._download_all,
            args=(urls, out_dir, mode),
            daemon=True,
        )
        self._worker.start()

    def _stop_download(self) -> None:
        self._stop_flag.set()
        self._log("Detención solicitada… (terminarán las descargas en curso)")

    def _download_one_url(
        self,
        base_cmd: list[str],
        url: str,
        out_dir: Path,
        format_mode: str,
        index: int,
        total: int,
    ) -> str:
        """Descarga una URL. Devuelve: ok | skipped | fail."""
        if self._stop_flag.is_set():
            return "fail"

        self._log(f"\n[{index}/{total}] {url}")
        try:
            info = fetch_video_info(base_cmd, url)
            basename = song_artist_basename(info)
            artist_folder = folder_artist_from_info(info)
            album_folder = folder_album_from_info(info)
            track_dir = ensure_artist_album_dir(out_dir, artist_folder, album_folder)
            self._link_catalog.add(basename, url)
            self.after(0, lambda: refresh_link_catalog_window(self))
            self._log(f"[{index}] Nombre: {basename}")
            self._log(f"[{index}] Carpeta: {artist_folder} / {album_folder}")

            existing = find_existing_download(track_dir, basename)
            if existing:
                self._log(f"[{index}] ⊘ Ya estaba descargada: {existing.name}")
                return "skipped"

            cmd = build_download_cmd(
                base_cmd, url, track_dir, format_mode, self._ffmpeg, basename
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
                return "fail"
            if code == 0:
                self._log(f"[{index}] ✓ Completado ({index}/{total})")
                audio_path = find_existing_download(track_dir, basename)
                if audio_path:
                    attach_lyrics_and_cover(
                        audio_path,
                        basename,
                        info,
                        self._ffmpeg,
                        self._log,
                    )
                else:
                    self._log(
                        f"[{index}] AVISO: audio OK pero no se encontró el archivo para extras"
                    )
                return "ok"
            self._log(f"[{index}] ✗ Error (código {code}) en {url}")
            return "fail"
        except FileNotFoundError:
            self._log(
                "✗ No se encontró yt-dlp. Instálalo con:\n"
                "  python -m pip install yt-dlp"
            )
            self._stop_flag.set()
            return "fail"
        except Exception as exc:  # noqa: BLE001
            self._log(f"[{index}] ✗ Excepción: {exc}")
            return "fail"

    def _download_all(self, urls: list[str], out_dir: Path, format_mode: str) -> None:
        base_cmd = find_yt_dlp()
        counters = {"ok": 0, "skipped": 0, "fail": 0}
        lock = threading.Lock()
        done = 0
        total = len(urls)

        self._log(f"Paralelo: hasta {PARALLEL_DOWNLOADS} descargas a la vez")

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
                result = fut.result()
                with lock:
                    if result in counters:
                        counters[result] += 1
                    done += 1
                    current = done
                self.after(0, lambda v=current: self.progress.configure(value=v))

        ok, skipped, fail = counters["ok"], counters["skipped"], counters["fail"]
        if self._stop_flag.is_set():
            self._log("Descargas detenidas por el usuario.")
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
