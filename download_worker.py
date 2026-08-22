"""
Worker de descargas en segundo plano (sin UI).
Cola dual (opción A):
  - Pool audio (×PARALLEL_DOWNLOADS): solo baja y coloca el archivo
  - Pool extras (×PARALLEL_DOWNLOADS): carátula/letra en paralelo
Así, mientras bajan pistas nuevas, se embeben extras de las ya listas.
Calidad de audio intacta.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from descargar_musica import (  # noqa: E402
    PARALLEL_DOWNLOADS,
    build_download_cmd,
    cache_lookup_existing,
    cache_remember,
    cleanup_staging_id,
    ensure_artist_album_dir,
    extract_youtube_id,
    find_ffmpeg,
    find_info_json,
    find_yt_dlp,
    folder_album_from_info,
    folder_artist_from_info,
    get_staging_dir,
    is_transient_yt_block,
    load_info_json_file,
    promote_staged_to_dest,
    scrub_delivery_artifacts,
    song_artist_basename,
)
from metadata_extras import attach_lyrics_and_cover  # noqa: E402
import download_state as state  # noqa: E402

# Mismo paralelismo para extras (no bloquea slots de audio)
PARALLEL_EXTRAS = PARALLEL_DOWNLOADS

_DOWNLOAD_PCT_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
_DOWNLOAD_DEST_RE = re.compile(r"\[download\] Destination:\s+(.+)")
_DOWNLOAD_SPEED_RE = re.compile(
    r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%.*?at\s+(\S+/s)",
    re.IGNORECASE,
)
_PROGRESS_MIN_STEP = 1  # % mínimo entre escrituras al IPC (UI actualiza in-place)
_PROGRESS_MIN_SECS = 0.35


@dataclass
class ExtrasTask:
    job_id: int
    audio_path: Path
    basename: str
    info: dict
    ffmpeg: Path | None


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # Archivo primero (canal real de la UI). print nunca debe tumbar el job
    # (en Windows stdout suele ser charmap/cp1252 y falla con ✗/✓/títulos).
    state.append_worker_log(line)
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


def os_getpid() -> int:
    import os

    return os.getpid()


def _label_from_dest(path_str: str) -> str | None:
    name = Path(path_str.strip().strip('"')).name
    if not name:
        return None
    # Quitar extensión típica de staging (%(id)s.ext)
    stem = Path(name).stem
    return stem or name


def run_yt_dlp_with_progress(
    cmd: list[str],
    job_id: int,
    label: str,
    *,
    staging: Path | None = None,
    video_id: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """
    Ejecuta yt-dlp leyendo la salida en vivo para % de descarga.
    No altera flags de calidad/formato (solo añade --newline al final).
    """
    run_cmd = list(cmd)
    if "--newline" not in run_cmd:
        run_cmd.insert(-1, "--newline")

    state.set_live_progress(job_id, 0, label, speed="—", speed_bps=None)
    last_pct = -_PROGRESS_MIN_STEP
    last_write = 0.0
    last_label_try = 0.0
    last_speed = "—"
    last_bps: float | None = None
    chunks: list[str] = []

    def _maybe_set(
        pct: float,
        lab: str,
        *,
        speed: str | None = None,
        speed_bps: float | None = None,
    ) -> None:
        nonlocal last_pct, last_write, last_speed, last_bps
        now = time.time()
        if speed:
            last_speed = speed
        if speed_bps is not None and speed_bps > 0:
            last_bps = speed_bps
        if pct < 100 and (pct - last_pct) < _PROGRESS_MIN_STEP and (
            now - last_write
        ) < _PROGRESS_MIN_SECS:
            return
        last_pct = pct
        last_write = now
        state.set_live_progress(
            job_id,
            pct,
            lab,
            speed=last_speed,
            speed_bps=last_bps,
        )

    def _try_label_from_info() -> None:
        nonlocal label, last_label_try
        now = time.time()
        if staging is None or not video_id:
            return
        if now - last_label_try < 1.0:
            return
        last_label_try = now
        info_path = staging / f"{video_id}.info.json"
        if not info_path.is_file():
            return
        try:
            info = load_info_json_file(info_path)
            got = song_artist_basename(info)
            if got:
                label = got
                state.set_live_progress(
                    job_id,
                    max(last_pct, 0),
                    label,
                    speed=last_speed,
                    speed_bps=last_bps,
                )
        except Exception:  # noqa: BLE001
            pass

    proc = subprocess.Popen(
        run_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    try:
        while True:
            if state.stop_requested():
                try:
                    proc.terminate()
                except OSError:
                    pass
                break
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            chunks.append(line)
            _try_label_from_info()
            sm = _DOWNLOAD_SPEED_RE.search(line)
            if sm:
                try:
                    pct = float(sm.group(1))
                except ValueError:
                    pct = 0.0
                speed_tok = sm.group(2)
                bps = state.parse_speed_to_bps(speed_tok)
                speed_txt = (
                    state.format_speed_bps(bps) if bps else speed_tok
                )
                _maybe_set(pct, label, speed=speed_txt, speed_bps=bps)
                continue
            m = _DOWNLOAD_PCT_RE.search(line)
            if m:
                try:
                    pct = float(m.group(1))
                except ValueError:
                    pct = 0.0
                _maybe_set(pct, label)
                continue
            d = _DOWNLOAD_DEST_RE.search(line)
            if d:
                got = _label_from_dest(d.group(1))
                if got and (not video_id or label == video_id):
                    label = got
                    state.set_live_progress(
                        job_id,
                        max(last_pct, 0),
                        label,
                        speed=last_speed,
                        speed_bps=last_bps,
                    )
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=5)
    finally:
        state.clear_live_progress(job_id)

    out = "".join(chunks)
    code = proc.returncode if proc.returncode is not None else 1
    return subprocess.CompletedProcess(run_cmd, code, out, "")


def download_audio_only(job) -> ExtrasTask | None:
    """
    Fase audio: staging → destino. Sin carátula/letra.
    Devuelve ExtrasTask si hay que embeber; None si skip/fail/cancel.
    """
    job_id = int(job["id"])
    url = str(job["url"])
    base_dir = Path(str(job["base_dir"]))
    format_mode = str(job["format_mode"])
    album_dir_raw = job["album_dir"]
    ffmpeg = find_ffmpeg()
    base_cmd = find_yt_dlp()

    try:
        video_id = extract_youtube_id(url)
        fixed_dest = Path(album_dir_raw) if album_dir_raw else None
        cache_root = fixed_dest if fixed_dest else base_dir
        live_label = video_id or url[-40:]

        hit = cache_lookup_existing(cache_root, video_id, fixed_dest=fixed_dest)
        if hit:
            basename, _existing = hit
            log(f"⊘ Ya estaba [{job_id}]: {basename}")
            state.update_job(job_id, state.STATUS_SKIPPED, basename=basename)
            return None

        staging_parent = fixed_dest if fixed_dest else base_dir
        staging = get_staging_dir(staging_parent)
        scrub_delivery_artifacts(staging_parent)

        cmd = build_download_cmd(
            base_cmd,
            url,
            staging,
            format_mode,
            ffmpeg,
            staging=True,
        )
        proc = None
        detail = ""
        for attempt in range(1, 4):
            proc = run_yt_dlp_with_progress(
                cmd,
                job_id,
                live_label,
                staging=staging,
                video_id=video_id,
            )
            if state.stop_requested():
                break
            if proc.returncode == 0:
                break
            detail = (proc.stderr or proc.stdout or "")[-300:]
            if is_transient_yt_block(detail) and attempt < 3:
                wait_s = 3 * attempt
                log(
                    f"AVISO [{job_id}]: bloqueo temporal YT, "
                    f"reintento {attempt}/2 en {wait_s}s…"
                )
                time.sleep(wait_s)
                continue
            break

        if state.stop_requested():
            cleanup_staging_id(staging, video_id)
            state.update_job(job_id, state.STATUS_CANCELLED)
            log(f"Cancelado [{job_id}]")
            return None
        assert proc is not None
        if proc.returncode != 0:
            cleanup_staging_id(staging, video_id)
            detail = detail or (proc.stderr or proc.stdout or "")[-300:]
            state.update_job(
                job_id,
                state.STATUS_FAILED,
                error=detail or f"code {proc.returncode}",
            )
            log(f"✗ Falló [{job_id}]: {detail[:120]}")
            return None

        info_path = find_info_json(staging, video_id)
        if info_path is None:
            cleanup_staging_id(staging, video_id)
            state.update_job(job_id, state.STATUS_FAILED, error="sin info.json")
            log(f"✗ Sin info.json [{job_id}]")
            return None

        info = load_info_json_file(info_path)
        if not video_id:
            video_id = str(info.get("id") or "") or None

        basename = song_artist_basename(info)
        if fixed_dest is not None:
            dest_dir = fixed_dest
            dest_dir.mkdir(parents=True, exist_ok=True)
            artist_folder = dest_dir.parent.name
            album_folder = dest_dir.name
        else:
            artist_folder = folder_artist_from_info(info)
            album_folder = folder_album_from_info(info)
            dest_dir = ensure_artist_album_dir(base_dir, artist_folder, album_folder)

        status, final_path, basename = promote_staged_to_dest(staging, dest_dir, info)
        cache_remember(
            cache_root,
            video_id,
            basename,
            artist_folder,
            album_folder,
        )

        if status == "skipped":
            state.update_job(job_id, state.STATUS_SKIPPED, basename=basename)
            log(f"⊘ Ya estaba [{job_id}]: {basename}")
            return None
        if status != "ok" or final_path is None:
            state.update_job(
                job_id, state.STATUS_FAILED, basename=basename, error="promote"
            )
            log(f"✗ Promote [{job_id}]")
            return None

        log(f"✓ Audio [{job_id}]: {basename} → extras en paralelo…")
        return ExtrasTask(
            job_id=job_id,
            audio_path=final_path,
            basename=basename,
            info=info,
            ffmpeg=ffmpeg,
        )
    except Exception as exc:  # noqa: BLE001
        state.clear_live_progress(job_id)
        state.update_job(job_id, state.STATUS_FAILED, error=str(exc)[:400])
        log(f"✗ Excepción audio [{job_id}]: {exc}")
        traceback.print_exc()
        return None


def run_extras_task(task: ExtrasTask) -> None:
    """Fase extras: carátula + letra (no bloquea el pool de audio)."""
    try:
        if state.stop_requested():
            state.update_job(
                task.job_id, state.STATUS_CANCELLED, basename=task.basename
            )
            log(f"Extras cancelados [{task.job_id}]")
            return
        attach_lyrics_and_cover(
            task.audio_path,
            task.basename,
            task.info,
            task.ffmpeg,
            log,
        )
        state.update_job(task.job_id, state.STATUS_DONE, basename=task.basename)
        log(f"✓ Listo [{task.job_id}]: {task.basename}")
    except Exception as exc:  # noqa: BLE001
        # Audio ya está; marcar done con aviso (extras no tumba la pista)
        state.update_job(task.job_id, state.STATUS_DONE, basename=task.basename)
        log(f"AVISO extras [{task.job_id}]: {exc} (audio OK)")


def _reap_done(futures: dict[Future, object]) -> list[object]:
    """Quita futures terminados; devuelve lista de resultados."""
    results: list[object] = []
    for fut in list(futures.keys()):
        if not fut.done():
            continue
        futures.pop(fut, None)
        try:
            results.append(fut.result())
        except Exception as exc:  # noqa: BLE001
            log(f"Worker future: {exc}")
    return results


def main() -> int:
    state.init_db()
    state.clear_worker_stop()
    state.clear_live_progress()
    state.write_pid(os_getpid())
    log(
        f"Worker activo pid={os_getpid()} "
        f"audio×{PARALLEL_DOWNLOADS} extras×{PARALLEL_EXTRAS}"
    )

    idle_rounds = 0
    audio_futs: dict[Future, object] = {}
    extras_futs: dict[Future, object] = {}

    audio_pool = ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS)
    extras_pool = ThreadPoolExecutor(max_workers=PARALLEL_EXTRAS)

    try:
        while True:
            if state.stop_requested():
                log("Stop solicitado: cancelando pendientes…")
                state.cancel_active_jobs()
                # Esperar a que terminen audios/extras en vuelo un poco
                break

            # 1) Audios terminados → encolar extras (libera slot de audio)
            for result in _reap_done(audio_futs):
                if isinstance(result, ExtrasTask):
                    fut = extras_pool.submit(run_extras_task, result)
                    extras_futs[fut] = result

            # 2) Extras terminados
            _reap_done(extras_futs)

            # 3) Rellenar slots de audio libres
            free_audio = PARALLEL_DOWNLOADS - len(audio_futs)
            if free_audio > 0 and not state.stop_requested():
                jobs = state.claim_pending_jobs(free_audio)
                for job in jobs:
                    fut = audio_pool.submit(download_audio_only, job)
                    audio_futs[fut] = job

            pending = state.queue_counts().get(state.STATUS_PENDING, 0)
            busy = bool(audio_futs) or bool(extras_futs) or pending > 0

            if not busy:
                idle_rounds += 1
                if idle_rounds >= 30:
                    log("Cola vacía: worker termina.")
                    break
                time.sleep(1)
                continue

            idle_rounds = 0
            time.sleep(0.25)

        # Drenar en vuelo al salir
        for result in _reap_done(audio_futs):
            if isinstance(result, ExtrasTask):
                run_extras_task(result)
        # esperar extras restantes
        while extras_futs:
            _reap_done(extras_futs)
            if extras_futs:
                time.sleep(0.2)
    finally:
        audio_pool.shutdown(wait=True, cancel_futures=False)
        extras_pool.shutdown(wait=True, cancel_futures=False)
        state.clear_live_progress()
        state.clear_pid()
        state.clear_worker_stop()
        log("Worker finalizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
