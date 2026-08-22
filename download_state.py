"""
Cola durable de descargas (SQLite) + control del worker en segundo plano.
Patrón industrial: UI encola; un proceso worker procesa; cerrar la UI no mata el worker.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Raíz del proyecto (junto a los .py)
ROOT = Path(__file__).resolve().parent
QUEUE_DIR = ROOT / ".download_queue"
DB_PATH = QUEUE_DIR / "queue.sqlite3"
PID_PATH = QUEUE_DIR / "worker.pid"
STOP_PATH = QUEUE_DIR / "stop.flag"
LOG_PATH = QUEUE_DIR / "worker.log"
WORKER_SCRIPT = ROOT / "download_worker.py"

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
    }
