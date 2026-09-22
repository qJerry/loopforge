"""LoopForge 内置自动调度循环。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .domain import utc_now
from .run_dispatch import schedule_dispatch_tick


TickFn = Callable[[Optional[Path]], Dict[str, Any]]


def schedule_tick_due(config_path: Optional[Path]) -> Dict[str, Any]:
    return schedule_dispatch_tick(config_path, respect_frequency=True)


class AutoScheduler:
    def __init__(self, config_path: Optional[Path], interval_seconds: int = 60, tick_fn: TickFn = schedule_tick_due) -> None:
        self.config_path = config_path
        self.interval_seconds = max(1, int(interval_seconds))
        self.tick_fn = tick_fn
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._tick_count = 0
        self._last_started_at: Optional[str] = None
        self._last_finished_at: Optional[str] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="loopforge-auto-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def tick_once(self) -> Dict[str, Any]:
        started_at = utc_now()
        with self._lock:
            self._tick_count += 1
            self._last_started_at = started_at
            self._last_error = None
        try:
            payload = self.tick_fn(self.config_path)
        except Exception as exc:
            payload = {"status": "failed", "summary": str(exc)}
            with self._lock:
                self._last_error = str(exc)
        with self._lock:
            self._last_finished_at = utc_now()
            self._last_result = payload
        return payload

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.running,
                "interval_seconds": self.interval_seconds,
                "tick_count": self._tick_count,
                "last_started_at": self._last_started_at,
                "last_finished_at": self._last_finished_at,
                "last_result": self._last_result,
                "last_error": self._last_error,
            }

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            self._stop.wait(self.interval_seconds)
