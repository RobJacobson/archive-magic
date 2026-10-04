"""Shared request-start gate and coordinated cooldowns."""

from __future__ import annotations

import threading
import time
from concurrent.futures import CancelledError
from typing import Callable

from archive_magic_fetch.models import CaptureIdentity
from archive_magic_fetch.runtime.report_progress import emit
from archive_magic_fetch.runtime.calculate_retry_delay import linear_backpressure_delay


class StartGate:
    """Smooth request starts and pause every worker on source-directed cooldowns.

    Each failure after a cooldown escalates the pause, including retries of
    the same capture: 60s, 120s, 180s, ... up to ten minutes, never shorter than
    ``Retry-After``. Concurrent failures during a pause share one level.
    A successful download after the pause resets escalation. A completion that
    arrives during the pause does not, and idle time alone does not either.
    """

    def __init__(
        self,
        starts_per_second: float,
        *,
        report: Callable[[str], None] = emit,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 0.0 if starts_per_second <= 0 else 1 / starts_per_second
        self._report = report
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._endpoint_starts: dict[str, float] = {}
        self._blocked_until = 0.0
        self._level = 0

    def wait(
        self, *, cancelled: threading.Event | None = None,
        endpoint: str = "", minimum_interval: float = 0.0,
    ) -> None:
        while True:
            check_cancelled(cancelled)
            with self._lock:
                now = self._clock()
                deadline = max(
                    self._next_start, self._blocked_until,
                    self._endpoint_starts.get(endpoint, 0.0),
                )
                if now >= deadline:
                    self._next_start = now + self._interval
                    if minimum_interval:
                        self._endpoint_starts[endpoint] = now + minimum_interval
                    return
            wait_or_cancel(deadline - now, cancelled=cancelled, sleep=self._sleep)

    def note_success(self) -> None:
        """A completed download outside an active cooldown clears escalation."""

        with self._lock:
            if self._level == 0 or self._clock() < self._blocked_until:
                return
            self._level = 0

    def pause(
        self,
        kind: str,
        retry_after: float | None,
        identity: CaptureIdentity,
        *,
        label: str | None = None,
    ) -> None:
        with self._lock:
            now = self._clock()
            if now >= self._blocked_until:
                self._level += 1
            delay = linear_backpressure_delay(self._level, retry_after)
            self._blocked_until = max(self._blocked_until, now + delay)
            remaining = self._blocked_until - now
            level = self._level
        source = label or kind
        self._report(
            f"{source} at {identity.timestamp}; "
            f"cooldown={delay:g}s, level={level}; "
            f"new starts paused for {remaining:g}s"
        )


def check_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise CancelledError("annual acquisition batch cancelled")


def wait_or_cancel(
    seconds: float,
    *,
    cancelled: threading.Event | None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wake real waits on cancellation; retain injected sleepers for virtual clocks."""

    check_cancelled(cancelled)
    if cancelled is not None and sleep is time.sleep:
        cancelled.wait(seconds)
    else:
        sleep(seconds)
    check_cancelled(cancelled)
