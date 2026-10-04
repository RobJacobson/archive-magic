"""Shared request-start gate and coordinated cooldowns."""

from __future__ import annotations

import threading
import time
from typing import Callable

from archive_magic_fetch.models import CaptureIdentity
from archive_magic_fetch.runtime.reporting import emit
from archive_magic_fetch.runtime.retry import linear_backpressure_delay


class StartGate:
    """Smooth request starts and pause every worker on source-directed cooldowns.

    Successive waves from different captures escalate with a linear 60s, 120s,
    180s, ... cooldown (never shorter than ``Retry-After``). Concurrent 429s in
    the same pause window share one level. Retries of a capture that already
    paused this wave stay at the current level. A non-backpressure completion
    after the pause clears resets the escalation.
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
        self._blocked_until = 0.0
        self._max_retry_after = 0.0
        self._level = 0
        self._wave_identities: set[CaptureIdentity] = set()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                deadline = max(self._next_start, self._blocked_until)
                if now >= deadline:
                    self._next_start = now + self._interval
                    return
            self._sleep(deadline - now)

    def note_success(self) -> None:
        """Reset escalation after a non-backpressure completion past the pause."""

        with self._lock:
            if self._clock() >= self._blocked_until:
                self._level = 0
                self._max_retry_after = 0.0
                self._wave_identities.clear()

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
                retry_from_wave = identity in self._wave_identities and self._level > 0
                if not retry_from_wave:
                    self._level += 1
                    self._max_retry_after = 0.0
                    self._wave_identities = set()
            self._wave_identities.add(identity)
            delay = linear_backpressure_delay(self._level, retry_after)
            self._max_retry_after = max(self._max_retry_after, delay)
            self._blocked_until = max(self._blocked_until, now + delay)
            maximum = self._max_retry_after
            remaining = self._blocked_until - now
            level = self._level
        source = label or kind
        if retry_after is not None and kind == "http":
            policy = f"Retry-After={retry_after:g}s, applied={delay:g}s"
        elif kind == "http":
            policy = f"Retry-After=absent, applied={delay:g}s"
        else:
            policy = f"cooldown={delay:g}s"
        self._report(
            f"{source} at {identity.timestamp}; "
            f"{policy}, level={level}, maximum={maximum:g}s; "
            f"new starts paused for {remaining:g}s"
        )
