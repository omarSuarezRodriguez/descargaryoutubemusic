"""
Cola durable de descargas (SQLite) + control del worker en segundo plano.
Patrón industrial: UI encola; un proceso worker procesa; cerrar la UI no mata el worker.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

# Raíz del proyecto (junto a los .py)
ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / ".download_queue"
DB_PATH = QUEUE_DIR / "queue.sqlite3"
PID_PATH = QUEUE_DIR / "worker.pid"
STOP_PATH = QUEUE_DIR / "stop.flag"
LOG_PATH = QUEUE_DIR / "worker.log"
PROGRESS_PATH = QUEUE_DIR / "live_progress.json"
WORKER_SCRIPT = ROOT / "download_worker.py"

_progress_lock = threading.Lock()

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


def ensure_queue_dir() -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR


def connect() -> sqlite3.Connection:
    ensure_queue_dir()
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY,
                app TEXT NOT NULL,
                format_mode TEXT NOT NULL,
                base_dir TEXT NOT NULL,
                created_at REAL NOT NULL,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                app TEXT NOT NULL,
                url TEXT NOT NULL,
                base_dir TEXT NOT NULL,
                format_mode TEXT NOT NULL,
                album_dir TEXT,
                status TEXT NOT NULL,
                basename TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES batches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
            """
        )


def _now() -> float:
    return time.time()


def create_batch(
    batch_id: str,
    app: str,
    format_mode: str,
    base_dir: str,
    note: str = "",
) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO batches(id, app, format_mode, base_dir, created_at, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (batch_id, app, format_mode, base_dir, _now(), note),
        )


def enqueue_track_jobs(
    batch_id: str,
    app: str,
    urls: list[str],
    base_dir: str,
    format_mode: str,
    album_dirs: list[str | None] | None = None,
) -> int:
    """Encola pistas. album_dirs[i] opcional (playlists). Devuelve cantidad insertada."""
    init_db()
    if album_dirs is None:
        album_dirs = [None] * len(urls)
    if len(album_dirs) != len(urls):
        raise ValueError("album_dirs debe coincidir con urls")
    now = _now()
    rows = [
        (
            batch_id,
            app,
            url,
            base_dir,
            format_mode,
            album_dirs[i],
            STATUS_PENDING,
            now,
            now,
        )
        for i, url in enumerate(urls)
    ]
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO jobs(
                batch_id, app, url, base_dir, format_mode, album_dir,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return len(rows)


def claim_pending_jobs(limit: int) -> list[sqlite3.Row]:
    """Marca hasta `limit` jobs pending → running y los devuelve (atómico)."""
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (STATUS_PENDING, limit),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return []
        now = _now()
        conn.executemany(
            """
            UPDATE jobs SET status = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            [(STATUS_RUNNING, now, i, STATUS_PENDING) for i in ids],
        )
        return conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall()


def update_job(
    job_id: int,
    status: str,
    basename: str | None = None,
    error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, basename = COALESCE(?, basename),
                error = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, basename, error, _now(), job_id),
        )


def cancel_active_jobs() -> int:
    """Cancela pending/running. Devuelve filas afectadas."""
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE jobs SET status = ?, updated_at = ?
            WHERE status IN (?, ?)
            """,
            (STATUS_CANCELLED, _now(), STATUS_PENDING, STATUS_RUNNING),
        )
        return int(cur.rowcount or 0)


def queue_counts() -> dict[str, int]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    out = {
        STATUS_PENDING: 0,
        STATUS_RUNNING: 0,
        STATUS_DONE: 0,
        STATUS_SKIPPED: 0,
        STATUS_FAILED: 0,
        STATUS_CANCELLED: 0,
    }
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    return out


def active_job_count() -> int:
    c = queue_counts()
    return c[STATUS_PENDING] + c[STATUS_RUNNING]


def recent_jobs(limit: int = 40) -> list[sqlite3.Row]:
    init_db()
    with connect() as conn:
        return conn.execute(
            """
            SELECT * FROM jobs ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def request_worker_stop() -> None:
    ensure_queue_dir()
    STOP_PATH.write_text("1", encoding="utf-8")


def clear_worker_stop() -> None:
    if STOP_PATH.exists():
        try:
            STOP_PATH.unlink()
        except OSError:
            pass


def stop_requested() -> bool:
    return STOP_PATH.is_file()


def read_pid() -> int | None:
    if not PID_PATH.is_file():
        return None
    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_pid(pid: int) -> None:
    ensure_queue_dir()
    PID_PATH.write_text(str(pid), encoding="utf-8")


def clear_pid() -> None:
    if PID_PATH.exists():
        try:
            PID_PATH.unlink()
        except OSError:
            pass


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # OpenProcess + wait 0 is heavy; use tasklist-less approach via os.kill(pid, 0) not on Windows.
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def worker_is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    if pid_is_running(pid):
        return True
    clear_pid()
    return False


def append_worker_log(line: str) -> None:
    ensure_queue_dir()
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError:
        pass


def read_worker_log_tail(max_lines: int = 80) -> list[str]:
    if not LOG_PATH.is_file():
        return []
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except OSError:
        return []


def _read_live_progress_unlocked() -> dict:
    if not PROGRESS_PATH.is_file():
        return {}
    try:
        raw = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _write_live_progress_unlocked(data: dict) -> None:
    ensure_queue_dir()
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROGRESS_PATH)


def format_speed_bps(bps: float | None) -> str:
    """Formato corto de velocidad (B/s → KiB/s / MiB/s)."""
    if bps is None or bps <= 0:
        return "—"
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MiB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KiB/s"
    return f"{bps:.0f} B/s"


def total_live_speed_bps(entries: list[dict] | None = None) -> float | None:
    """Suma de velocidades de las descargas en curso (throughput total)."""
    if entries is None:
        entries = read_live_progress()
    total = 0.0
    any_speed = False
    for e in entries or []:
        bps = e.get("speed_bps")
        try:
            bps_f = float(bps) if bps is not None else 0.0
        except (TypeError, ValueError):
            bps_f = 0.0
        if bps_f > 0:
            total += bps_f
            any_speed = True
    return total if any_speed else None


def format_eta_seconds(secs: float | None) -> str:
    """Texto corto de tiempo restante."""
    if secs is None:
        return ""
    try:
        secs_f = float(secs)
    except (TypeError, ValueError):
        return ""
    if secs_f < 0 or secs_f != secs_f:  # NaN
        return ""
    secs_i = int(round(secs_f))
    if secs_i <= 0:
        return "Restante: ~0s"
    if secs_i < 60:
        return f"Restante: ~{secs_i}s"
    mins, sec = divmod(secs_i, 60)
    if mins < 60:
        return f"Restante: ~{mins}m {sec:02d}s"
    hours, mins = divmod(mins, 60)
    return f"Restante: ~{hours}h {mins:02d}m"


def batch_progress_fraction(
    *,
    finished: int,
    total: int,
    live_entries: list[dict] | None,
) -> float:
    """Progreso del lote en [0, 1+] (finished + fracciones en vivo)."""
    if total <= 0:
        return 0.0
    live = live_entries or []
    frac_live = 0.0
    for e in live:
        try:
            pct = int(e.get("pct") or 0)
        except (TypeError, ValueError):
            pct = 0
        frac_live += max(0, min(100, pct)) / 100.0
    return (max(0, int(finished)) + frac_live) / float(total)


def estimate_batch_eta_seconds(
    *,
    finished: int,
    total: int,
    live_entries: list[dict] | None,
    started_at: float | None,
    now: float | None = None,
    baseline_progress: float = 0.0,
) -> float | None:
    """
    ETA del lote: ritmo observado desde baseline / tiempo transcurrido.
    baseline_progress permite ETA correcto al reanudar UI a mitad de lote.
    """
    if started_at is None or total <= 0:
        return None
    now_f = float(now if now is not None else _now())
    elapsed = now_f - float(started_at)
    if elapsed < 1.0:
        return None
    progress = batch_progress_fraction(
        finished=finished, total=total, live_entries=live_entries
    )
    if progress >= 0.999:
        return 0.0
    try:
        base = float(baseline_progress)
    except (TypeError, ValueError):
        base = 0.0
    gained = progress - max(0.0, base)
    if gained <= 0.001:
        return None
    return elapsed * (1.0 - progress) / gained


def parse_speed_to_bps(speed_token: str) -> float | None:
    """Convierte '1.20MiB/s' / '800KiB/s' a bytes/s."""
    m = re.match(
        r"^\s*([0-9]*\.?[0-9]+)\s*([KMG]?i?B)/s\s*$",
        speed_token or "",
        re.IGNORECASE,
    )
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower()
    mult = 1.0
    if unit.startswith("ki"):
        mult = 1024.0
    elif unit.startswith("mi"):
        mult = 1024.0 ** 2
    elif unit.startswith("gi"):
        mult = 1024.0 ** 3
    elif unit == "kb":
        mult = 1000.0
    elif unit == "mb":
        mult = 1000.0 ** 2
    elif unit == "gb":
        mult = 1000.0 ** 3
    return value * mult


def set_live_progress(
    job_id: int,
    pct: float,
    label: str,
    *,
    speed: str | None = None,
    speed_bps: float | None = None,
) -> None:
    """Progreso en vivo de una pista (IPC worker → UI)."""
    pct_i = max(0, min(100, int(round(float(pct)))))
    label = (label or f"#{job_id}").strip() or f"#{job_id}"
    speed_txt = (speed or "").strip()
    if speed_bps is not None and speed_bps > 0 and not speed_txt:
        speed_txt = format_speed_bps(speed_bps)
    with _progress_lock:
        data = _read_live_progress_unlocked()
        prev = data.get(str(int(job_id)))
        if isinstance(prev, dict):
            if not speed_txt:
                speed_txt = str(prev.get("speed") or "")
            if speed_bps is None:
                try:
                    speed_bps = float(prev.get("speed_bps") or 0) or None
                except (TypeError, ValueError):
                    speed_bps = None
        entry = {
            "pct": pct_i,
            "label": label[:70],
            "ts": _now(),
        }
        if speed_txt:
            entry["speed"] = speed_txt[:24]
        if speed_bps is not None and speed_bps > 0:
            entry["speed_bps"] = float(speed_bps)
        data[str(int(job_id))] = entry
        try:
            _write_live_progress_unlocked(data)
        except OSError:
            pass


def clear_live_progress(job_id: int | None = None) -> None:
    """Quita una pista del progreso en vivo, o todas si job_id es None."""
    with _progress_lock:
        if job_id is None:
            data: dict = {}
        else:
            data = _read_live_progress_unlocked()
            data.pop(str(int(job_id)), None)
        try:
            _write_live_progress_unlocked(data)
        except OSError:
            pass


def read_live_progress() -> list[dict]:
    """Lista ordenada [{job_id, pct, label, speed, speed_bps}, ...] en curso."""
    with _progress_lock:
        data = _read_live_progress_unlocked()
    out: list[dict] = []
    for key, val in data.items():
        if not isinstance(val, dict):
            continue
        try:
            jid = int(key)
        except (TypeError, ValueError):
            continue
        try:
            bps = float(val["speed_bps"]) if val.get("speed_bps") is not None else None
        except (TypeError, ValueError):
            bps = None
        out.append(
            {
                "job_id": jid,
                "pct": int(val.get("pct") or 0),
                "label": str(val.get("label") or f"#{jid}"),
                "speed": str(val.get("speed") or ""),
                "speed_bps": bps,
            }
        )
    out.sort(key=lambda x: x["job_id"])
    return out


def format_live_progress_lines(entries: list[dict] | None = None) -> list[str]:
    """
    Bloque mínimo para el log (se reescribe in-place en la UI):
      ↓ 45%  1.2 MiB/s  canción - artista
      ↓ 12%  800 KiB/s  otra - artista
      ≈ avg 1.0 MiB/s
    """
    if entries is None:
        entries = read_live_progress()
    lines: list[str] = []
    speeds: list[float] = []
    for e in entries:
        pct = max(0, min(100, int(e.get("pct") or 0)))
        label = str(e.get("label") or f"#{e.get('job_id', '?')}")
        bps = e.get("speed_bps")
        try:
            bps_f = float(bps) if bps is not None else None
        except (TypeError, ValueError):
            bps_f = None
        speed = str(e.get("speed") or "").strip() or format_speed_bps(bps_f)
        if bps_f is not None and bps_f > 0:
            speeds.append(bps_f)
        lines.append(f"↓ {pct}%  {speed}  {label}")
    if lines:
        if speeds:
            avg = sum(speeds) / len(speeds)
            lines.append(f"≈ avg {format_speed_bps(avg)}")
        else:
            lines.append("≈ avg —")
    return lines


_WORKER_TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")


def summarize_worker_event(line: str) -> str | None:
    """
    Extrae un evento corto del worker.log para el Registro mínimo.
    Ignora ruido (carátula, letra, extras, worker activo, etc.).
    """
    msg = _WORKER_TS_RE.sub("", (line or "").strip())
    if not msg:
        return None
    m = re.match(r"✓ Listo\s*\[\d+\]:\s*(.+)$", msg)
    if m:
        return f"✓ {m.group(1).strip()}"
    m = re.match(r"⊘ Ya estaba\s*\[\d+\]:\s*(.+)$", msg)
    if m:
        return f"⊘ {m.group(1).strip()}"
    m = re.match(r"✗ Falló\s*\[\d+\]:\s*(.+)$", msg)
    if m:
        detail = m.group(1).strip()
        if len(detail) > 60:
            detail = detail[:57] + "…"
        return f"✗ {detail}"
    m = re.match(r"✗\s+(.+)$", msg)
    if m and not msg.startswith("✗ Falló"):
        # Otros fallos relevantes (sin info.json, promote, etc.)
        if any(
            k in msg.lower()
            for k in ("info.json", "promote", "excepción", "no se")
        ):
            detail = m.group(1).strip()
            if len(detail) > 60:
                detail = detail[:57] + "…"
            return f"✗ {detail}"
    if msg.startswith("Cancelado ["):
        return "⊘ Cancelado"
    return None


def build_minimal_registry_text(
    *,
    finished: int,
    total: int,
    events: list[str],
    live_entries: list[dict] | None,
    summary: str | None = None,
    active: bool = True,
) -> str:
    """
    Texto completo del Registro (mínimo pero con flujo):
      Descargando 5/40
      ✓ canción lista
      ⊘ ya estaba
      ↓ 38%  …  (en curso)
      ≈ avg …
    o solo el resumen al terminar.
    """
    if summary and not active:
        return summary.rstrip() + "\n"

    parts: list[str] = []
    total_i = max(int(total), 1)
    finished_i = max(0, int(finished))
    parts.append(f"Descargando {finished_i}/{total_i}")
    if events:
        parts.extend(events[-40:])
    live_lines = format_live_progress_lines(live_entries or [])
    if live_lines:
        if events:
            parts.append("")  # separador visual
        parts.extend(live_lines)
    return "\n".join(parts) + "\n"


def ensure_worker_running() -> bool:
    """
    Arranca download_worker.py en proceso separado si no hay uno vivo.
    Devuelve True si ya corría o se lanzó.
    """
    ensure_queue_dir()
    init_db()
    if worker_is_running():
        return True
    clear_worker_stop()
    creationflags = 0
    if sys.platform == "win32":
        # Proceso independiente de la UI (sobrevive al cerrar la ventana)
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    try:
        proc = subprocess.Popen(
            [sys.executable, str(WORKER_SCRIPT)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        write_pid(proc.pid)
        append_worker_log(f"[control] worker iniciado pid={proc.pid}")
        return True
    except Exception as exc:  # noqa: BLE001
        append_worker_log(f"[control] no se pudo iniciar worker: {exc}")
        return False


def batch_progress(batch_id: str) -> dict[str, int]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS n FROM jobs
            WHERE batch_id = ?
            GROUP BY status
            """,
            (batch_id,),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    out = {
        STATUS_PENDING: 0,
        STATUS_RUNNING: 0,
        STATUS_DONE: 0,
        STATUS_SKIPPED: 0,
        STATUS_FAILED: 0,
        STATUS_CANCELLED: 0,
        "total": int(total["n"]) if total else 0,
    }
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    finished = (
        out[STATUS_DONE]
        + out[STATUS_SKIPPED]
        + out[STATUS_FAILED]
        + out[STATUS_CANCELLED]
    )
    out["finished"] = finished
    return out


def snapshot_status() -> dict:
    c = queue_counts()
    return {
        "worker_running": worker_is_running(),
        "worker_pid": read_pid(),
        "counts": c,
        "active": c[STATUS_PENDING] + c[STATUS_RUNNING],
        "log_tail": read_worker_log_tail(50),
        "live_progress": read_live_progress(),
    }
