"""Bounded playback scheduler with smooth pacing and delayed retries."""

from __future__ import annotations

import heapq
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import mktime_tz, parsedate_tz
from queue import Full, Queue
from typing import Callable, Optional, Sequence

from .models import (
    DEFAULT_429_COOLDOWN_S,
    DEFAULT_CONNECTION_REFUSAL_COOLDOWN_S,
    MAX_429_COOLDOWN_S,
    MAX_CONNECTION_REFUSAL_COOLDOWN_S,
    MAX_CONNECTIONS,
    MAX_PLAYBACK_ATTEMPTS,
    MAX_RETRY_DELAY_S,
    PLAYBACK_REQUESTS_PER_SECOND,
    RESULT_QUEUE_SIZE,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    RunMetrics,
    UnresolvedFailure,
)
from .warc import (
    classify_playback_error,
    download_exact_for_identity,
)


Clock = Callable[[], float]
Sleep = Callable[[float], None]


@dataclass(order=True)
class ReadyJob:
    """Deterministic ready-queue entry."""

    sort_key: tuple[str, str, str, str, str]
    identity: CaptureIdentity = field(compare=False)
    attempt: int = field(compare=False, default=1)


@dataclass(order=True)
class DelayedJob:
    """Retry eligible after a monotonic deadline."""

    ready_at: float
    identity: CaptureIdentity = field(compare=False)
    attempt: int = field(compare=False)
    category: str = field(compare=False, default="retry")


@dataclass(frozen=True)
class JobSuccess:
    identity: CaptureIdentity
    result: PlaybackResult


@dataclass(frozen=True)
class JobFailure:
    identity: CaptureIdentity
    category: FailureCategory
    message: str
    retryable: bool
    attempt: int
    retry_after: Optional[float] = None


DownloadFn = Callable[[object, CaptureIdentity], PlaybackResult]


class PlaybackScheduler:
    """Own start pacing, connection slots, delayed retries, and global gates."""

    def __init__(
        self,
        client_factory: Callable[[], object],
        *,
        identities: Sequence[CaptureIdentity],
        max_connections: int = MAX_CONNECTIONS,
        requests_per_second: float = PLAYBACK_REQUESTS_PER_SECOND,
        max_attempts: int = MAX_PLAYBACK_ATTEMPTS,
        result_queue_size: int = RESULT_QUEUE_SIZE,
        metrics: Optional[RunMetrics] = None,
        download_fn: DownloadFn = download_exact_for_identity,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        self._client_factory = client_factory
        self._max_connections = max_connections
        self._start_interval = 1.0 / requests_per_second
        self._max_attempts = max_attempts
        self._download_fn = download_fn
        self._clock = clock
        self._sleep = sleep
        self.metrics = metrics or RunMetrics()
        self._ready: list[ReadyJob] = []
        for identity in identities:
            heapq.heappush(
                self._ready,
                ReadyJob(sort_key=identity.sort_key(), identity=identity),
            )
        self._retry_ready: list[ReadyJob] = []
        self._delayed: list[DelayedJob] = []
        self._active_connections = 0
        self._blocked_until = 0.0
        self._consecutive_429 = 0
        self._connection_blocked_until = 0.0
        self._connection_refusal_waves = 0
        self._next_start_at = 0.0
        self._results: Queue[JobSuccess | JobFailure | None] = Queue(
            maxsize=result_queue_size
        )
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._consumer_pending = 0
        self._local = threading.local()
        self._clients: list[object] = []

    def run(self) -> None:
        """Process all ready/delayed jobs until drained."""

        with ThreadPoolExecutor(max_workers=self._max_connections) as pool:
            futures: set[Future] = set()
            while not self._stop.is_set():
                now = self._clock()
                self._promote_delayed(now)

                with self._lock:
                    idle = (
                        not self._ready
                        and not self._retry_ready
                        and not self._delayed
                        and self._active_connections == 0
                        and self._consumer_pending == 0
                    )
                if idle and not futures:
                    break

                # Wait for capacity / readiness. First attempts always precede
                # promoted retries so delayed work cannot jump the ready queue.
                has_runnable = bool(self._ready or self._retry_ready)
                if (
                    self._active_connections >= self._max_connections
                    or not has_runnable
                ):
                    if self._delayed and not has_runnable:
                        delay = max(0.0, self._delayed[0].ready_at - now)
                        self._wait_briefly(min(delay, 0.05))
                    else:
                        self._wait_briefly(0.01)
                    self._collect_futures(futures)
                    continue

                wait = self._gate_wait(now)
                if wait > 0:
                    self._sleep(min(wait, 0.05))
                    self.metrics.rate_gate_wait_s += min(wait, 0.05)
                    if now < self._blocked_until:
                        self.metrics.cooldown_wait_s += min(wait, 0.05)
                    continue

                if self._ready:
                    job = heapq.heappop(self._ready)
                else:
                    job = heapq.heappop(self._retry_ready)
                with self._lock:
                    self._active_connections += 1
                    self.metrics.peak_connections = max(
                        self.metrics.peak_connections,
                        self._active_connections,
                    )
                self.metrics.playback_starts += 1
                self._next_start_at = self._clock() + self._start_interval
                future = pool.submit(self._worker, job)
                futures.add(future)
                self._collect_futures(futures)

            # drain
            while futures:
                self._collect_futures(futures)
                if futures:
                    self._wait_briefly(0.01)

        self._put_result(None)
        for client in self._clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            exit_fn = getattr(client, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)

    def results(self):
        """Yield completed successes/failures until a terminal sentinel."""

        while True:
            item = self._results.get()
            if item is None:
                break
            yield item

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, identity: CaptureIdentity, *, attempt: int = 1) -> None:
        """Add a first-attempt identity while the scheduler is running."""

        with self._lock:
            heapq.heappush(
                self._ready,
                ReadyJob(
                    sort_key=identity.sort_key(),
                    identity=identity,
                    attempt=attempt,
                ),
            )

    def acknowledge(self) -> None:
        """Mark one consumed result as fully handled by the writer."""

        with self._lock:
            self._consumer_pending = max(0, self._consumer_pending - 1)

    def _worker(self, job: ReadyJob) -> JobSuccess | JobFailure:
        identity = job.identity
        request_started_at = self._clock()
        try:
            client = self._thread_client()
            result = self._download_fn(client, identity)
            success = JobSuccess(identity=identity, result=result)
            self.metrics.playback_completions += 1
            self.metrics.playback_bytes += len(result.body)
            self._consecutive_429 = 0
            self._put_result(success)
            return success
        except Exception as error:  # noqa: BLE001 - boundary classification
            category, retryable = classify_playback_error(error)
            retry_after = _retry_after_from_error(error)
            if _is_rate_limit_error(error):
                self._note_429(retry_after, identity=identity, error=error)
            elif _is_connection_refused_error(error):
                self._note_connection_refusal(
                    identity=identity,
                    error=error,
                    request_started_at=request_started_at,
                )
            failure = JobFailure(
                identity=identity,
                category=category,
                message=str(error) or type(error).__name__,
                retryable=retryable,
                attempt=job.attempt,
                retry_after=retry_after,
            )
            self.metrics.bump_attempt(category.value)
            self._handle_failure(failure)
            return failure
        finally:
            with self._lock:
                self._active_connections = max(0, self._active_connections - 1)

    def _handle_failure(self, failure: JobFailure) -> None:
        if (
            failure.retryable
            and failure.attempt < self._max_attempts
        ):
            delay = self._retry_delay(failure)
            heapq.heappush(
                self._delayed,
                DelayedJob(
                    ready_at=self._clock() + delay,
                    identity=failure.identity,
                    attempt=failure.attempt + 1,
                    category=failure.category.value,
                ),
            )
            message = failure.message or failure.category.value
            print(
                f"  retry: deferred {delay:g}s "
                f"(attempt {failure.attempt}/{self._max_attempts}) "
                f"{failure.identity.original_url} [{message}]",
                flush=True,
            )
            return
        # Permanent or exhausted: surface to consumer.
        self._put_result(failure)

    def _retry_delay(self, failure: JobFailure) -> float:
        if failure.retry_after is not None:
            return min(float(failure.retry_after), MAX_RETRY_DELAY_S)
        delay = 5 * (2 ** failure.attempt)
        return float(min(delay, MAX_RETRY_DELAY_S))

    def _note_429(
        self,
        retry_after: Optional[float],
        *,
        identity: CaptureIdentity,
        error: Optional[BaseException] = None,
    ) -> None:
        self._consecutive_429 += 1
        header_delay = _retry_after_from_response(
            getattr(error, "response", None) if error is not None else None
        )
        if header_delay is not None:
            cooldown = min(header_delay, MAX_429_COOLDOWN_S)
            source = "Retry-After"
        elif retry_after is not None:
            cooldown = min(float(retry_after), MAX_429_COOLDOWN_S)
            source = "error"
        else:
            # IA often omits the header; wayback recommends a fixed pause.
            cooldown = DEFAULT_429_COOLDOWN_S
            source = "default"
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + cooldown,
        )
        detail = ""
        if error is not None:
            message = str(error) or type(error).__name__
            detail = f" [{type(error).__name__}: {message}]"
        print(
            f"  rate limit: pausing {cooldown:g}s ({source}, "
            f"consecutive={self._consecutive_429}) "
            f"{identity.original_url}{detail}",
            flush=True,
        )

    def _note_connection_refusal(
        self,
        *,
        identity: CaptureIdentity,
        error: BaseException,
        request_started_at: float,
    ) -> None:
        """Globally pause after one TCP-refusal wave.

        Workers already using a connection slot can report the same refusal
        concurrently. Only the first refusal after the previous cooldown starts
        a new wave and doubles the fallback delay.
        """

        now = self._clock()
        with self._lock:
            # A request that began before this wave's cooldown expired belongs
            # to that same wave, even if its failure arrives after the timer.
            if request_started_at < self._connection_blocked_until:
                return
            self._connection_refusal_waves += 1
            cooldown = min(
                DEFAULT_CONNECTION_REFUSAL_COOLDOWN_S
                * (2 ** (self._connection_refusal_waves - 1)),
                MAX_CONNECTION_REFUSAL_COOLDOWN_S,
            )
            self._connection_blocked_until = now + cooldown
            self._blocked_until = max(
                self._blocked_until,
                self._connection_blocked_until,
            )
            wave = self._connection_refusal_waves

        message = str(error) or type(error).__name__
        print(
            f"  connection refused: pausing all requests {cooldown:g}s "
            f"(wave={wave}) {identity.original_url} "
            f"[{type(error).__name__}: {message}]",
            flush=True,
        )

    def _promote_delayed(self, now: float) -> None:
        while self._delayed and self._delayed[0].ready_at <= now:
            delayed = heapq.heappop(self._delayed)
            heapq.heappush(
                self._retry_ready,
                ReadyJob(
                    sort_key=delayed.identity.sort_key(),
                    identity=delayed.identity,
                    attempt=delayed.attempt,
                ),
            )

    def _gate_wait(self, now: float) -> float:
        with self._lock:
            blocked = max(0.0, self._blocked_until - now)
        spacing = max(0.0, self._next_start_at - now)
        return max(blocked, spacing)

    def _thread_client(self):
        active = getattr(self._local, "client", None)
        if active is not None:
            return active
        client = self._client_factory()
        enter = getattr(client, "__enter__", None)
        active = enter() if callable(enter) else client
        if active is None:
            active = client
        self._local.client = active
        with self._lock:
            self._clients.append(client)
        return active

    def _collect_futures(self, futures: set[Future]) -> None:
        done = {future for future in futures if future.done()}
        for future in done:
            futures.remove(future)
            # Propagate unexpected worker crashes.
            exc = future.exception()
            if exc is not None and not isinstance(exc, Exception):
                raise exc

    def _wait_briefly(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(seconds)

    def _put_result(self, item: JobSuccess | JobFailure | None) -> None:
        """Enqueue a result without deadlocking after stop()."""

        if item is not None:
            with self._lock:
                self._consumer_pending += 1
        while True:
            try:
                self._results.put(item, timeout=0.1)
                return
            except Full:
                if item is not None and self._stop.is_set():
                    with self._lock:
                        self._consumer_pending = max(
                            0, self._consumer_pending - 1
                        )
                    return
                continue


def _is_rate_limit_error(error: BaseException) -> bool:
    """Return whether error represents an HTTP 429 / rate-limit response.

    Avoid bare ``\"429\" in message`` checks: capture timestamps and URLs often
    contain that digit sequence (e.g. ``20080429``) and were falsely tripping
    the global cooldown on ordinary connection errors.
    """

    for candidate in _iter_error_chain(error):
        if "RateLimit" in type(candidate).__name__:
            return True
        if getattr(candidate, "status_code", None) == 429:
            return True
        response = getattr(candidate, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        message = str(candidate).lower()
        if "rate limit" in message or "too many requests" in message:
            return True
        if re.search(r"\b429\b", message):
            return True
    return False


def _is_connection_refused_error(error: BaseException) -> bool:
    """Return whether an error chain ends in a refused TCP connection."""

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, ConnectionRefusedError):
            return True
        if "connection refused" in str(candidate).lower():
            return True
    return False


def _iter_error_chain(error: BaseException):
    """Yield an error and its wrapped causes without looping forever."""

    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        nested = getattr(current, "cause", None)
        if isinstance(nested, BaseException):
            current = nested
            continue
        if isinstance(current.__cause__, BaseException):
            current = current.__cause__
            continue
        if isinstance(current.__context__, BaseException):
            current = current.__context__
            continue
        break


_RETRY_AFTER_IN_MESSAGE = re.compile(
    r"retry after\s+(\d+(?:\.\d+)?)\s*s",
    re.IGNORECASE,
)


def _parse_retry_after_header(value: object) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(max(0.0, value))
    if not isinstance(value, str):
        return None
    try:
        return float(max(0, int(value)))
    except ValueError:
        retry_date = parsedate_tz(value)
        if retry_date is None:
            return None
        return float(max(0, mktime_tz(retry_date) - int(time.time())))


def _retry_after_from_response(response: object) -> Optional[float]:
    headers = getattr(response, "headers", None) or {}
    if not headers:
        return None
    return _parse_retry_after_header(
        headers.get("Retry-After") or headers.get("retry-after")
    )


def _retry_after_from_error(error: BaseException) -> Optional[float]:
    """Return the pause recommended by a rate-limit or HTTP error, if any."""

    for candidate in _iter_error_chain(error):
        value = getattr(candidate, "retry_after", None)
        parsed = _parse_retry_after_header(value)
        if parsed is not None:
            return parsed

        parsed = _retry_after_from_response(getattr(candidate, "response", None))
        if parsed is not None:
            return parsed

        match = _RETRY_AFTER_IN_MESSAGE.search(str(candidate))
        if match:
            return float(match.group(1))
    return None


def failure_from_job(failure: JobFailure) -> UnresolvedFailure:
    """Convert a scheduler permanent failure into a ledger entry."""

    return UnresolvedFailure(
        identity=failure.identity,
        category=failure.category,
        message=failure.message,
    )
