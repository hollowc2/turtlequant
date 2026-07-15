"""Bounded best-effort notification worker; trading never waits on Discord."""

from __future__ import annotations

import logging
from collections.abc import Callable
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any

logger = logging.getLogger(__name__)


class NotificationQueue:
    def __init__(self, maxsize: int = 100) -> None:
        self._queue: Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = Queue(maxsize)
        self._stopped = Event()
        self._worker = Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
        try:
            self._queue.put_nowait((func, args, kwargs))
            return True
        except Full:
            logger.warning("Notification queue full; dropping notification")
            return False

    def close(self) -> None:
        self._stopped.set()
        self._worker.join(timeout=5)

    def _run(self) -> None:
        while not self._stopped.is_set() or not self._queue.empty():
            try:
                func, args, kwargs = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                func(*args, **kwargs)
            except Exception:
                logger.exception("Notification failed")
            finally:
                self._queue.task_done()
