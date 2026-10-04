"""Transport pacing, counters, and flushed request traces."""

from __future__ import annotations

import csv
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from requests.adapters import HTTPAdapter

from archive_magic_fetch.models import CaptureIdentity

TRACE_COLUMNS = (
    "request_id",
    "start_utc",
    "duration_ms",
    "status",
    "capture_time",
    "digest",
    "attempt",
    "request_in_attempt",
    "starts_last_1s",
    "starts_last_60s",
    "method",
    "pid",
    "thread",
    "retry_after",
    "error",
    "url",
    "location",
)


def format_trace_time(value: datetime) -> str:
    """Format a UTC timestamp rounded to the nearest millisecond."""

    rounded = value.astimezone(timezone.utc) + timedelta(microseconds=500)
    return rounded.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def format_capture_time(timestamp: str) -> str:
    """Use the same human-readable capture date as the progress log."""

    if not timestamp:
        return ""
    return (
        f"{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}T"
        f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}"
    )


class RequestStats:
    """One run's transport counters and optional flushed CSV trace.

    Windows are rolling (now - window, now], not calendar buckets. Starts
    count attempted HTTP sends, including connection failures. Response timing
    ends at receipt of headers; streaming body reads happen later. A row is
    appended on completion, using the timestamp and counters from its start.
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
        self._pending: dict[int, dict[str, object]] = {}
        self._stream: TextIO | None = (
            trace_path.open("x", encoding="utf-8", newline="", buffering=1)
            if trace_path is not None
            else None
        )
        self._writer = (
            csv.DictWriter(self._stream, fieldnames=TRACE_COLUMNS, lineterminator="\n")
            if self._stream is not None
            else None
        )
        if self._writer is not None:
            self._writer.writeheader()

    @contextmanager
    def attempt(self, identity: CaptureIdentity, number: int) -> Iterator[None]:
        self._local.capture = identity
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

    def start(self, request) -> tuple[int, float]:
        # This is called at the adapter boundary, immediately before the transport send.
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
            self._local.request_in_attempt = (
                getattr(self._local, "request_in_attempt", 0) + 1
            )
            if self._writer is not None:
                capture = getattr(self._local, "capture", None)
                self._pending[self._total] = {
                    "request_id": self._total,
                    "start_utc": format_trace_time(datetime.now(timezone.utc)),
                    "capture_time": format_capture_time(capture.timestamp)
                    if capture
                    else "",
                    "digest": capture.payload_digest[-6:] if capture else "",
                    "attempt": getattr(self._local, "attempt", None),
                    "request_in_attempt": self._local.request_in_attempt,
                    "starts_last_1s": counts["starts_last_1s"],
                    "starts_last_60s": counts["starts_last_60s"],
                    "method": request.method,
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "url": request.url,
                }
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
            row = self._pending.pop(request_id, None)
            if row is not None and self._writer is not None:
                self._writer.writerow(
                    {
                        **row,
                        "duration_ms": round((now - started) * 1000),
                        "status": status,
                        "retry_after": headers.get("Retry-After"),
                        "location": headers.get("Location"),
                        "error": type(error).__name__ if error is not None else None,
                    }
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
            if self._writer is not None:
                for row in self._pending.values():
                    self._writer.writerow({**row, "error": "Interrupted"})
                self._pending.clear()
                self._writer = None
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        self._report(
            f"playback HTTP: requests={counts['requests_total']}, "
            f"peak/1s={counts['peak_starts_1s']}, "
            f"peak/60s={counts['peak_starts_60s']}, 429s={counts['http_429']}"
        )


class InstrumentedHTTPAdapter(HTTPAdapter):
    """Pace and observe each transport call, with urllib3 retries disabled."""

    def __init__(self, stats: RequestStats) -> None:
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
