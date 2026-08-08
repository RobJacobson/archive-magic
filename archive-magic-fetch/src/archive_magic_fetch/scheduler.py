"""Bounded playback scheduler with smooth pacing and delayed retries."""

from __future__ import annotations

import heapq
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import mktime_tz, parsedate_tz
from enum import Enum
from queue import Full, Queue
from typing import Callable, Optional, Sequence

from .models import (
    CONNECTION_REFUSED_MAX_RETRIES,
    CONNECTION_REFUSED_RETRY_S,
    DEFAULT_429_COOLDOWN_S,
    MAX_429_COOLDOWN_S,
    MAX_CONNECTIONS,
    MAX_PLAYBACK_ATTEMPTS,
    MAX_RETRY_DELAY_S,
    PLAYBACK_REQUESTS_PER_SECOND,
    RESULT_QUEUE_SIZE,
    TRUNCATION_PAUSE_S,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    RunMetrics,
    UnresolvedFailure,
    is_invalid_uri_payload_digest,
    wayback_url,
)
from .warc import (
    classify_playback_error,
    download_exact_for_identity,
)


Clock = Callable[[], float]
Sleep = Callable[[float], None]


@dataclass
class PlaybackProgress:
    """Deferred terminal progress for playback downloads."""

    total: int
    _count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _slots: dict[str, int] = field(default_factory=dict, repr=False)

    def request_finished(
        self,
        url: str,
        duration: float,
        *,
        attempt: int = 1,
        slot_key: str,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            if slot_key not in self._slots:
                self._count += 1
                self._slots[slot_key] = self._count
            number = self._slots[slot_key]
            total = self.total
            width = len(str(total))
        retry = f" (retry {attempt})" if attempt > 1 else ""
        print(
            f"  {number:{width}d}/{total}: {url}{retry} ({duration:.1f}s)",
            flush=True,
        )
        if detail:
            indent = " " * (2 * width + 5)
            print(f"{indent}{detail}", flush=True)

    def note_additional_work(self, n: int = 1) -> None:
        """Increase the denominator when deferred captures become downloads."""

        if n < 1:
            return
        with self._lock:
            self.total += n


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
        progress: Optional[PlaybackProgress] = None,
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
        self._progress = progress
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
        self._consecutive_backpressure = 0
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
                    slept = min(wait, 0.05)
                    self._sleep(slept)
                    self.metrics.rate_gate_wait_s += slept
                    if now < self._blocked_until:
                        self.metrics.cooldown_wait_s += slept
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

    def note_additional_work(self, n: int = 1) -> None:
        """Account for captures promoted to downloads after the initial plan."""

        if self._progress is not None:
            self._progress.note_additional_work(n)

    def acknowledge(self) -> None:
        """Mark one consumed result as fully handled by the writer."""

        with self._lock:
            self._consumer_pending = max(0, self._consumer_pending - 1)

    def _log_download(
        self,
        *,
        url: str,
        slot_key: str,
        attempt: int,
        started_at: float,
        detail: str | None = None,
    ) -> None:
        if self._progress is not None:
            self._progress.request_finished(
                url,
                self._clock() - started_at,
                attempt=attempt,
                slot_key=slot_key,
                detail=detail,
            )

    def _worker(self, job: ReadyJob) -> JobSuccess | JobFailure:
        identity = job.identity
        slot_key = f"{identity.timestamp}\0{identity.original_url}"
        url = wayback_url(identity.timestamp, identity.original_url)
        started_at = self._clock()
        try:
            if is_invalid_uri_payload_digest(identity.payload_digest):
                failure = JobFailure(
                    identity=identity,
                    category=FailureCategory.UNAVAILABLE,
                    message="CDX digest is IA Invalid URI stub",
                    retryable=False,
                    attempt=job.attempt,
                )
                self._log_download(
                    url=url,
                    slot_key=slot_key,
                    attempt=job.attempt,
                    started_at=started_at,
                    detail="Skipped (invalid URI)",
                )
                self.metrics.bump_attempt(failure.category.value)
                self._handle_failure(failure, will_retry=False, delay=0.0)
                return failure
            client = self._thread_client()
            result = self._download_fn(client, identity)
            success = JobSuccess(identity=identity, result=result)
            self.metrics.playback_completions += 1
            self.metrics.playback_bytes += len(result.body)
            self._consecutive_backpressure = 0
            self._log_download(
                url=url,
                slot_key=slot_key,
                attempt=job.attempt,
                started_at=started_at,
                detail=None if result.digest_matched else "digest mismatch kept",
            )
            self._put_result(success)
            return success
        except Exception as error:  # noqa: BLE001 - boundary classification
            category, retryable = classify_playback_error(error)
            retry_after = _retry_after_from_error(error)
            backpressure = _classify_backpressure(error)
            if backpressure is not None:
                self._note_backpressure(backpressure, error=error)
            elif category == FailureCategory.TRUNCATED:
                self._note_truncation()
            failure = JobFailure(
                identity=identity,
                category=category,
                message=str(error) or type(error).__name__,
                retryable=retryable,
                attempt=job.attempt,
                retry_after=retry_after,
            )
            will_retry, delay = self._retry_plan(failure, error)
            self._log_download(
                url=url,
                slot_key=slot_key,
                attempt=job.attempt,
                started_at=started_at,
                detail=_format_response_line(
                    error=error,
                    category=category,
                    will_retry=will_retry,
                    retry_delay=delay,
                ),
            )
            self.metrics.bump_attempt(category.value)
            self._handle_failure(failure, will_retry=will_retry, delay=delay)
            return failure
        finally:
            with self._lock:
                self._active_connections = max(0, self._active_connections - 1)

    def _handle_failure(
        self,
        failure: JobFailure,
        *,
        will_retry: bool,
        delay: float,
    ) -> None:
        if will_retry:
            heapq.heappush(
                self._delayed,
                DelayedJob(
                    ready_at=self._clock() + delay,
                    identity=failure.identity,
                    attempt=failure.attempt + 1,
                    category=failure.category.value,
                ),
            )
            return
        self._put_result(failure)

    def _retry_plan(
        self,
        failure: JobFailure,
        error: BaseException,
    ) -> tuple[bool, float]:
        backpressure = _classify_backpressure(error)
        if backpressure is not None:
            if backpressure.kind == BackpressureKind.TCP:
                if failure.attempt <= CONNECTION_REFUSED_MAX_RETRIES:
                    return True, CONNECTION_REFUSED_RETRY_S
                return False, 0.0
            if failure.retryable and failure.attempt < self._max_attempts:
                return True, self._retry_delay(failure)
            return False, 0.0
        if failure.category == FailureCategory.TRUNCATED:
            return False, 0.0
        if failure.retryable and failure.attempt < self._max_attempts:
            return True, self._retry_delay(failure)
        return False, 0.0

    def _retry_delay(self, failure: JobFailure) -> float:
        if failure.retry_after is not None:
            return min(float(failure.retry_after), MAX_RETRY_DELAY_S)
        delay = 5 * (2 ** failure.attempt)
        return float(min(delay, MAX_RETRY_DELAY_S))

    def _note_backpressure(
        self,
        signal: BackpressureSignal,
        *,
        error: Optional[BaseException] = None,
    ) -> None:
        self._consecutive_backpressure += 1
        if signal.kind == BackpressureKind.TCP:
            cooldown = CONNECTION_REFUSED_RETRY_S
        else:
            header_delay = _retry_after_from_response(
                getattr(error, "response", None) if error is not None else None
            )
            if header_delay is not None:
                cooldown = min(header_delay, MAX_429_COOLDOWN_S)
            elif signal.retry_after is not None:
                cooldown = min(float(signal.retry_after), MAX_429_COOLDOWN_S)
            else:
                cooldown = DEFAULT_429_COOLDOWN_S
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + cooldown,
        )

    def _note_truncation(self) -> None:
        """Handle a permanent incomplete payload without pacing delay.

        When ``TRUNCATION_PAUSE_S`` is positive, open a short global gate pause
        (historical mitigation for suspected TCP backpressure). Current policy
        is skip-and-continue (pause is 0); still recycle the thread client in
        case the transfer left the connection half-closed.
        """

        if TRUNCATION_PAUSE_S > 0:
            self._blocked_until = max(
                self._blocked_until,
                self._clock() + TRUNCATION_PAUSE_S,
            )
        self._reset_thread_client()

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

    def _reset_thread_client(self) -> None:
        active = getattr(self._local, "client", None)
        if active is not None:
            close = getattr(active, "close", None)
            if callable(close):
                close()
        self._local.client = None

    def _collect_futures(self, futures: set[Future]) -> None:
        done = {future for future in futures if future.done()}
        for future in done:
            futures.remove(future)
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


def _format_response_line(
    *,
    error: BaseException,
    category: FailureCategory,
    will_retry: bool,
    retry_delay: float,
) -> str:
    if category == FailureCategory.TRUNCATED:
        return "Skipped (truncated response)"
    if category == FailureCategory.UNAVAILABLE:
        return "Skipped (unavailable)"
    backpressure = _classify_backpressure(error)
    if backpressure is not None:
        if backpressure.kind == BackpressureKind.TCP:
            if will_retry:
                return (
                    f"Warning: Connection Refused (TCP backpressure), "
                    f"retrying in {int(CONNECTION_REFUSED_RETRY_S)}s"
                )
            return (
                "Error: Connection Refused (TCP backpressure), "
                f"giving up after {CONNECTION_REFUSED_MAX_RETRIES} retries"
            )
        delay = int(retry_delay) if retry_delay else int(DEFAULT_429_COOLDOWN_S)
        if will_retry:
            return (
                f"Warning: Rate Limited (HTTP backpressure), "
                f"retrying in {delay}s"
            )
        return "Error: Rate Limited (HTTP backpressure), continuing"
    label = _short_error_label(error, category)
    if will_retry:
        delay_text = (
            f"{int(retry_delay)}s"
            if retry_delay >= 1
            else f"{retry_delay:.1f}s"
        )
        return f"Warning: {label}, retrying in {delay_text}"
    return f"Error: {label}, continuing"


def _short_error_label(error: BaseException, category: FailureCategory) -> str:
    if category == FailureCategory.EXACT_MISMATCH:
        return "Exact Mismatch"
    if category == FailureCategory.DIGEST_VALIDATION:
        return "Digest Validation Failed"
    if category == FailureCategory.BLOCKED:
        return "Blocked"
    if category == FailureCategory.UNAVAILABLE:
        return "Unavailable"
    name = type(error).__name__
    if name.endswith("Error"):
        name = name[: -len("Error")]
    elif name.endswith("Exception"):
        name = name[: -len("Exception")]
    if name:
        return name
    return category.value.replace("_", " ").title()


class BackpressureKind(Enum):
    """Where Internet Archive signaled overload."""

    HTTP = "http"
    TCP = "tcp"


@dataclass(frozen=True)
class BackpressureSignal:
    """IA throttling at the application or transport layer."""

    kind: BackpressureKind
    retry_after: Optional[float] = None


def _classify_backpressure(error: BaseException) -> Optional[BackpressureSignal]:
    """Return IA backpressure at HTTP or TCP layer, if present.

    Internet Archive may rate-limit with HTTP 429 or by refusing TCP
    connections. Both share the same scheduler cooldown gate. Avoid bare
    ``\"429\" in message`` checks: capture timestamps and URLs often contain
    that digit sequence (e.g. ``20080429``).
    """

    tcp = False
    http = False
    retry_after: Optional[float] = None

    for candidate in _iter_error_chain(error):
        if isinstance(candidate, ConnectionRefusedError):
            tcp = True
            continue
        message = str(candidate).lower()
        if "connection refused" in message:
            tcp = True
            continue

        candidate_http = False
        if "RateLimit" in type(candidate).__name__:
            candidate_http = True
        elif getattr(candidate, "status_code", None) == 429:
            candidate_http = True
        else:
            response = getattr(candidate, "response", None)
            if getattr(response, "status_code", None) == 429:
                candidate_http = True
            elif "rate limit" in message or "too many requests" in message:
                candidate_http = True
            elif re.search(r"\b429\b", message):
                candidate_http = True

        if candidate_http:
            http = True
            parsed = _retry_after_from_error(candidate)
            if parsed is not None:
                retry_after = parsed

    if http:
        return BackpressureSignal(
            kind=BackpressureKind.HTTP,
            retry_after=retry_after,
        )
    if tcp:
        return BackpressureSignal(kind=BackpressureKind.TCP)
    return None


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
