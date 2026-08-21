"""
Descargador de audio desde YouTube Music / YouTube.
Pega varios enlaces (uno por línea) y descarga MP3 en orden.
"""

from __future__ import annotations

import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


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
        matches = sorted(local.glob("**/ffmpeg.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
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


def build_download_cmd(
    base_cmd: list[str],
    url: str,
    out_dir: Path,
    format_mode: str,
    ffmpeg: Path | None,
) -> list[str]:
    """Arma el comando yt-dlp según MP3 (convertir) o WebM (tal cual)."""
    cmd = [
        *base_cmd,
        "-f",
        "bestaudio",
        "-o",
        str(out_dir / "%(title)s.%(ext)s"),
        "--no-playlist",
    ]
    if format_mode == "webm":
        # Audio original de YouTube (casi siempre Opus en .webm)
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
        self.title("Descargar YouTube Music (MP3)")
        self.geometry("720x560")
        self.minsize(560, 420)

        self.download_dir = tk.StringVar(
            value=str(Path.home() / "Downloads" / "YouTubeMusic")
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
            text="WebM (tal cual YouTube)",
            variable=self.format_mode,
            value="webm",
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
                "AVISO: no se encontró ffmpeg. Hace falta para convertir a MP3.\n"
                "El modo WebM (tal cual) funciona sin ffmpeg.\n"
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
        mode_label = "MP3 (compatible)" if mode == "mp3" else "WebM (tal cual YouTube)"
        if mode == "mp3" and not self._ffmpeg:
            messagebox.showwarning(
                "Falta ffmpeg",
                "No se encontró ffmpeg.\n\n"
                "Sin ffmpeg no se puede convertir a MP3.\n"
                "Usa el modo WebM o instala: winget install Gyan.FFmpeg",
            )
            self._set_busy(False)
            return
        self._log(f"Iniciando descarga de {len(urls)} enlace(s)…")
        self._log(f"Formato: {mode_label}")
        self._log(f"Carpeta: {out_dir}")

        self._worker = threading.Thread(
            target=self._download_all,
            args=(urls, out_dir, mode),
            daemon=True,
        )
        self._worker.start()

    def _stop_download(self) -> None:
        self._stop_flag.set()
        self._log("Detención solicitada… (terminará el archivo actual)")

    def _download_all(self, urls: list[str], out_dir: Path, format_mode: str) -> None:
        base_cmd = find_yt_dlp()
        ok = 0
        fail = 0

        for index, url in enumerate(urls, start=1):
            if self._stop_flag.is_set():
                self._log("Descargas detenidas por el usuario.")
                break

            self._log(f"\n[{index}/{len(urls)}] {url}")
            cmd = build_download_cmd(
                base_cmd, url, out_dir, format_mode, self._ffmpeg
            )

            try:
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
                        self._log("Proceso actual detenido.")
                        break
                    line = line.rstrip()
                    if line:
                        self._log(line)
                code = process.wait()
                if self._stop_flag.is_set():
                    fail += 1
                    break
                if code == 0:
                    ok += 1
                    self._log(f"✓ Completado ({index}/{len(urls)})")
                else:
                    fail += 1
                    self._log(f"✗ Error (código {code}) en {url}")
            except FileNotFoundError:
                fail += 1
                self._log(
                    "✗ No se encontró yt-dlp. Instálalo con:\n"
                    "  python -m pip install yt-dlp"
                )
                break
            except Exception as exc:  # noqa: BLE001
                fail += 1
                self._log(f"✗ Excepción: {exc}")

            self.after(0, lambda v=index: self.progress.configure(value=v))

        self._log(f"\nListo. OK: {ok} | Fallidos: {fail}")
        self.after(0, lambda: self._set_busy(False))
        if ok and not self._stop_flag.is_set():
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Descarga terminada",
                    f"Se descargaron {ok} archivo(s).\nFallidos: {fail}\n\nCarpeta:\n{out_dir}",
                ),
            )


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
