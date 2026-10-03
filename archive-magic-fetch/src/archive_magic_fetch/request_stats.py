"""Measure playback HTTP sends, including redirects and recovery requests."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from requests.adapters import HTTPAdapter

from .identity import identity_to_dict
from .models import CaptureIdentity


class PlaybackRequestStats:
    """One run's transport counters and optional flushed JSONL trace.

    Windows are rolling (now - window, now], not calendar buckets. Starts
    count attempted HTTP sends, including connection failures. Response timing
    ends at receipt of headers; streaming body reads happen later.
    """

    def __init__(
        self,
        wait: Callable[[], None],
        *,
        report: Callable[[str], None],
        trace_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wait = wait
        self._report = report
        self._clock = clock
        self._lock = threading.Lock()
        self._local = threading.local()
        self._recent: deque[float] = deque()
        self._total = 0
        self._peak_second = 0
        self._peak_minute = 0
        self._http_429 = 0
        self._stream: TextIO | None = (
            trace_path.open("x", encoding="utf-8", buffering=1)
            if trace_path is not None else None
        )

    @contextmanager
    def attempt(self, identity: CaptureIdentity, number: int) -> Iterator[None]:
        self._local.capture = identity_to_dict(identity)
        self._local.attempt = number
        self._local.request_in_attempt = 0
        try:
            yield
        finally:
            self._local.capture = None
            self._local.attempt = None

    def _counts(self, now: float) -> dict[str, int]:
        while self._recent and self._recent[0] <= now - 60:
            self._recent.popleft()
        return {
            "requests_total": self._total,
            "starts_last_1s": sum(t > now - 1 for t in self._recent),
            "starts_last_60s": len(self._recent),
            "peak_starts_1s": self._peak_second,
            "peak_starts_60s": self._peak_minute,
            "http_429": self._http_429,
        }

    def _write(self, event: str, **fields) -> None:
        if self._stream is not None:
            self._stream.write(json.dumps({
                "event": event,
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "pid": os.getpid(),
                "thread": threading.current_thread().name,
                **fields,
            }, ensure_ascii=True) + "\n")

    def start(self, request) -> tuple[int, float]:
        # This is called at the adapter boundary, after Wayback's own limiter.
        # Never hold the stats lock while waiting: in-flight responses must
        # still be able to finish and trigger a pool-wide cooldown.
        self._wait()
        with self._lock:
            now = self._clock()
            self._total += 1
            self._recent.append(now)
            counts = self._counts(now)
            self._peak_second = max(self._peak_second, counts["starts_last_1s"])
            self._peak_minute = max(self._peak_minute, counts["starts_last_60s"])
            self._local.request_in_attempt = getattr(self._local, "request_in_attempt", 0) + 1
            self._write(
                "request_start", request_id=self._total, monotonic_s=now,
                method=request.method, url=request.url,
                capture=getattr(self._local, "capture", None),
                attempt=getattr(self._local, "attempt", None),
                request_in_attempt=self._local.request_in_attempt,
                **self._counts(now),
            )
            return self._total, now

    def finish(self, ticket: tuple[int, float], *, response=None, error=None) -> None:
        request_id, started = ticket
        with self._lock:
            now = self._clock()
            status = response.status_code if response is not None else None
            headers = response.headers if response is not None else {}
            if status == 429:
                self._http_429 += 1
            counts = self._counts(now)
            self._write(
                "request_end", request_id=request_id, monotonic_s=now,
                elapsed_to_headers_s=now - started, status=status,
                retry_after=headers.get("Retry-After"),
                location=headers.get("Location"),
                error=type(error).__name__ if error is not None else None,
                **counts,
            )
        if status == 429:
            self._report(
                f"playback HTTP 429 request #{request_id}: "
                f"last 1s={counts['starts_last_1s']}, "
                f"last 60s={counts['starts_last_60s']}, "
                f"total={counts['requests_total']}"
            )

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return self._counts(self._clock())

    def close(self) -> None:
        with self._lock:
            counts = self._counts(self._clock())
            self._write("summary", **counts)
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        self._report(
            f"playback HTTP: requests={counts['requests_total']}, "
            f"peak/1s={counts['peak_starts_1s']}, "
            f"peak/60s={counts['peak_starts_60s']}, 429s={counts['http_429']}"
        )


class PlaybackHTTPAdapter(HTTPAdapter):
    """Pace and observe each transport call, with urllib3 retries disabled."""

    def __init__(self, stats: PlaybackRequestStats) -> None:
        super().__init__(max_retries=0)
        self._stats = stats

    def send(self, request, **kwargs):
        ticket = self._stats.start(request)
        try:
            response = super().send(request, **kwargs)
        except Exception as error:
            self._stats.finish(ticket, error=error)
            raise
        self._stats.finish(ticket, response=response)
        return response
