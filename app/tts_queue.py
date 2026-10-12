from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from .providers import tts_cache_key

TTS_WORKERS = 3

_pool = ThreadPoolExecutor(max_workers=TTS_WORKERS, thread_name_prefix="tts-queue")
_pending: dict[str, Future] = {}
_guard = threading.Lock()


def enqueue(provider: str, voice: str, text: str, work: Callable[[], object]) -> Future | None:
    """Queue background synthesis; returns None if the same text/voice is already queued or running."""
    key = tts_cache_key(provider, text, voice, 1.0)
    with _guard:
        if key in _pending:
            return None
        future = _pool.submit(work)
        _pending[key] = future

    def forget(_: Future) -> None:
        with _guard:
            _pending.pop(key, None)

    future.add_done_callback(forget)
    return future


def pending_count() -> int:
    with _guard:
        return len(_pending)
