"""Playback pacing, backpressure, retries, and persistent worker clients."""

from __future__ import annotations

import errno
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Sequence

from .identity import is_invalid_uri_payload_digest
from .models import (
    CaptureIdentity,
    FailureCategory,
    ParsedCapture,
    PlaybackResult,
    UnresolvedFailure,
)
from .playback import classify_playback_error
from .policy import (
    BACKPRESSURE_COOLDOWN_S,
    MAX_PLAYBACK_ATTEMPTS,
    PLAYBACK_STARTS_PER_SECOND,
    PLAYBACK_WORKERS,
)
from .retry import parse_retry_after


@dataclass(frozen=True)
class DownloadOutcome:
    result: PlaybackResult | None
    failure: UnresolvedFailure | None
    attempts: int
    elapsed_s: float
    categories: tuple[str, ...]


def _default_report(message: str) -> None:
    print(message, flush=True)


class StartGate:
    """Smooth request starts and pause every worker on IA backpressure."""

    def __init__(
        self,
        starts_per_second: float,
        *,
        report: Callable[[str], None] = _default_report,
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

    def wait(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                deadline = max(self._next_start, self._blocked_until)
                if now >= deadline:
                    if now >= self._blocked_until:
                        self._max_retry_after = 0.0
                    self._next_start = now + self._interval
                    return
            self._sleep(deadline - now)

    def pause(
        self,
        kind: str,
        retry_after: float | None,
        identity: CaptureIdentity,
    ) -> None:
        requested = retry_after or BACKPRESSURE_COOLDOWN_S
        with self._lock:
            now = self._clock()
            if now >= self._blocked_until:
                self._max_retry_after = 0.0
            self._max_retry_after = max(self._max_retry_after, requested)
            self._blocked_until = max(
                self._blocked_until,
                now + self._max_retry_after,
            )
            maximum = self._max_retry_after
            remaining = self._blocked_until - now
        source = "HTTP 429" if kind == "http" else "TCP connection refused"
        policy = (
            f"Retry-After={retry_after:g}s, applied={requested:g}s"
            if retry_after is not None and kind == "http"
            else (
                f"Retry-After=absent, applied={requested:g}s"
                if kind == "http"
                else f"cooldown={requested:g}s"
            )
        )
        self._report(
            f"rate limit: {source} at {identity.timestamp}; "
            f"{policy}, maximum={maximum:g}s; "
            f"new starts paused for {remaining:g}s"
        )


class PlaybackWorkers:
    """A bounded worker pool with one persistent client per worker."""

    def __init__(
        self,
        client_factory: Callable,
        download_fn: Callable,
        *,
        sleep: Callable[[float], None],
        pace: bool,
        report: Callable[[str], None] = _default_report,
    ) -> None:
        self._client_factory = client_factory
        self._download_fn = download_fn
        self._sleep = sleep
        self._gate = StartGate(
            PLAYBACK_STARTS_PER_SECOND if pace else 0,
            report=report,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=PLAYBACK_WORKERS,
            thread_name_prefix="playback",
        )
        self._local = threading.local()
        self._owners: list[object] = []
        self._owners_lock = threading.Lock()

    def close(self) -> None:
        self._executor.shutdown()
        for owner in self._owners:
            exit_fn = getattr(owner, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
                continue
            close = getattr(owner, "close", None)
            if callable(close):
                close()

    def submit(
        self,
        fn: Callable[[Sequence[ParsedCapture]], object],
        group: Sequence[ParsedCapture],
    ) -> Future:
        return self._executor.submit(fn, group)

    def download(self, identity: CaptureIdentity) -> DownloadOutcome:
        if is_invalid_uri_payload_digest(identity.payload_digest):
            return DownloadOutcome(
                result=None,
                failure=UnresolvedFailure(
                    identity=identity,
                    category=FailureCategory.UNAVAILABLE,
                    message="CDX digest is IA Invalid URI stub",
                ),
                attempts=0,
                elapsed_s=0.0,
                categories=(),
            )

        started = time.monotonic()
        categories: list[str] = []
        for attempt in range(1, MAX_PLAYBACK_ATTEMPTS + 1):
            self._gate.wait()
            try:
                result = self._download_fn(self._client(), identity)
            except Exception as error:  # noqa: BLE001 - network boundary
                category, retryable = classify_playback_error(error)
                categories.append(category.value)
                backpressure = backpressure_signal(error)
                if backpressure is not None:
                    self._gate.pause(*backpressure, identity)
                if retryable and attempt < MAX_PLAYBACK_ATTEMPTS:
                    if backpressure is None:
                        self._sleep(
                            retry_after_from_error(error)
                            or float(5 * (2 ** (attempt - 1)))
                        )
                    continue
                return DownloadOutcome(
                    result=None,
                    failure=UnresolvedFailure(
                        identity=identity,
                        category=category,
                        message=str(error) or type(error).__name__,
                    ),
                    attempts=attempt,
                    elapsed_s=time.monotonic() - started,
                    categories=tuple(categories),
                )
            return DownloadOutcome(
                result=result,
                failure=None,
                attempts=attempt,
                elapsed_s=time.monotonic() - started,
                categories=tuple(categories),
            )
        raise AssertionError("playback retry loop did not terminate")

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is not None:
            return client
        owner = self._client_factory()
        enter = getattr(owner, "__enter__", None)
        client = enter() if callable(enter) else owner
        self._local.client = client if client is not None else owner
        with self._owners_lock:
            self._owners.append(owner)
        return self._local.client


def iter_error_chain(error: BaseException):
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        nested = getattr(current, "cause", None)
        current = (
            nested
            if isinstance(nested, BaseException)
            else current.__cause__ or current.__context__
        )


def retry_after_from_error(error: BaseException) -> float | None:
    delays: list[float] = []
    for candidate in iter_error_chain(error):
        values = [getattr(candidate, "retry_after", None)]
        response = getattr(candidate, "response", None)
        headers = getattr(response, "headers", None) or {}
        values.append(headers.get("Retry-After") or headers.get("retry-after"))
        for value in values:
            parsed = parse_retry_after(value)
            if parsed is not None:
                delays.append(parsed)
    return max(delays, default=None)


def backpressure_signal(error: BaseException) -> tuple[str, float | None] | None:
    """Recognize HTTP 429 and refused TCP connections through wrapper chains."""

    http = False
    tcp = False
    for candidate in iter_error_chain(error):
        name = type(candidate).__name__
        message = str(candidate).lower()
        response = getattr(candidate, "response", None)
        if (
            "RateLimit" in name
            or getattr(candidate, "status_code", None) == 429
            or getattr(response, "status_code", None) == 429
            or "rate limit" in message
            or "too many requests" in message
        ):
            http = True
        if (
            isinstance(candidate, ConnectionRefusedError)
            or getattr(candidate, "errno", None) == errno.ECONNREFUSED
            or "connection refused" in message
        ):
            tcp = True
    if http:
        return "http", retry_after_from_error(error)
    if tcp:
        return "tcp", BACKPRESSURE_COOLDOWN_S
    return None
