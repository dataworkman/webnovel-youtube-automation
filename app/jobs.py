from __future__ import annotations

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .db import connect, execute, one, utc_now

ProgressFn = Callable[[int, int, str], None]

ACTIVE = ("QUEUED", "RUNNING")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
_start_lock = threading.Lock()


def recover_interrupted() -> None:
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'FAILED', error = 'Server restarted while the job was running.', finished_at = ? WHERE status IN ('QUEUED', 'RUNNING')",
            (now,),
        )
        conn.execute("UPDATE renders SET status = 'FAILED', error_log = 'Server restarted while rendering.' WHERE status = 'RENDERING'")
        conn.execute("UPDATE episodes SET status = 'SUBTITLES_READY' WHERE status = 'RENDERING'")


def active_job(episode_id: int) -> dict | None:
    return one("SELECT * FROM jobs WHERE episode_id = ? AND status IN ('QUEUED', 'RUNNING') ORDER BY id DESC LIMIT 1", (episode_id,))


def latest_job(episode_id: int) -> dict | None:
    return one("SELECT * FROM jobs WHERE episode_id = ? ORDER BY id DESC LIMIT 1", (episode_id,))


def start_job(episode_id: int, kind: str, label: str, fn: Callable[[ProgressFn], None]) -> int | None:
    """Runs fn in the background. Returns None if the episode already has an active job."""
    with _start_lock:
        if active_job(episode_id):
            return None
        job_id = execute(
            "INSERT INTO jobs (episode_id, kind, label, status, created_at) VALUES (?, ?, ?, 'QUEUED', ?)",
            (episode_id, kind, label, utc_now()),
        )
    _pool.submit(_run, job_id, fn)
    return job_id


def _run(job_id: int, fn: Callable[[ProgressFn], None]) -> None:
    def progress(current: int, total: int, message: str = "") -> None:
        execute(
            "UPDATE jobs SET progress_current = ?, progress_total = ?, message = ? WHERE id = ?",
            (current, total, message, job_id),
        )

    execute("UPDATE jobs SET status = 'RUNNING' WHERE id = ?", (job_id,))
    try:
        fn(progress)
    except Exception as exc:
        execute(
            "UPDATE jobs SET status = 'FAILED', error = ?, finished_at = ? WHERE id = ?",
            (error_text(exc), utc_now(), job_id),
        )
        return
    execute("UPDATE jobs SET status = 'SUCCEEDED', message = '', progress_current = progress_total, finished_at = ? WHERE id = ?", (utc_now(), job_id))


def error_text(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or exc.stdout or str(exc))[-2000:]
    return str(exc)[-2000:]
