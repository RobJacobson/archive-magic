"""Bounded playback scheduler with smooth pacing and delayed retries."""

from __future__ import annotations

import heapq
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Empty, Full, Queue
from typing import Callable, Optional, Sequence

from .models import (
    DEFAULT_429_COOLDOWN_S,
    MAX_429_COOLDOWN_S,
    MAX_IN_FLIGHT,
    MAX_PLAYBACK_ATTEMPTS,
    MAX_RETRY_DELAY_S,
    PLAYBACK_START_INTERVAL_S,
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
    """Own start pacing, in-flight slots, delayed retries, and 429 gate."""

    def __init__(
        self,
        client_factory: Callable[[], object],
        *,
        identities: Sequence[CaptureIdentity],
        max_in_flight: int = MAX_IN_FLIGHT,
        start_interval: float = PLAYBACK_START_INTERVAL_S,
        max_attempts: int = MAX_PLAYBACK_ATTEMPTS,
        result_queue_size: int = RESULT_QUEUE_SIZE,
        metrics: Optional[RunMetrics] = None,
        download_fn: DownloadFn = download_exact_for_identity,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._client_factory = client_factory
        self._max_in_flight = max_in_flight
        self._start_interval = start_interval
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
        self._delayed: list[DelayedJob] = []
        self._in_flight = 0
        self._blocked_until = 0.0
        self._consecutive_429 = 0
        self._next_start_at = 0.0
        self._results: Queue[JobSuccess | JobFailure | None] = Queue(
            maxsize=result_queue_size
        )
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._local = threading.local()
        self._clients: list[object] = []

    def run(self) -> None:
        """Process all ready/delayed jobs until drained."""

        with ThreadPoolExecutor(max_workers=self._max_in_flight) as pool:
            futures: set[Future] = set()
            while not self._stop.is_set():
                now = self._clock()
                self._promote_delayed(now)

                with self._lock:
                    idle = (
                        not self._ready
                        and not self._delayed
                        and self._in_flight == 0
                    )
                if idle and not futures:
                    break

                # Wait for capacity / readiness.
                if self._in_flight >= self._max_in_flight or not self._ready:
                    if self._delayed and not self._ready:
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

                job = heapq.heappop(self._ready)
                with self._lock:
                    self._in_flight += 1
                    self.metrics.peak_in_flight = max(
                        self.metrics.peak_in_flight,
                        self._in_flight,
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

        self._results.put(None)
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

    def _worker(self, job: ReadyJob) -> JobSuccess | JobFailure:
        identity = job.identity
        try:
            client = self._thread_client()
            result = self._download_fn(client, identity)
            success = JobSuccess(identity=identity, result=result)
            self.metrics.playback_completions += 1
            self.metrics.playback_bytes += len(result.body)
            self._results.put(success)
            return success
        except Exception as error:  # noqa: BLE001 - boundary classification
            category, retryable = classify_playback_error(error)
            retry_after = getattr(error, "retry_after", None)
            if category == FailureCategory.RETRY_EXHAUSTED and "429" in str(error):
                self._note_429(retry_after)
            failure = JobFailure(
                identity=identity,
                category=category,
                message=str(error) or type(error).__name__,
                retryable=retryable,
                attempt=job.attempt,
                retry_after=float(retry_after) if retry_after else None,
            )
            self.metrics.bump_attempt(category.value)
            self._handle_failure(failure)
            return failure
        finally:
            with self._lock:
                self._in_flight = max(0, self._in_flight - 1)

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
            return
        # Permanent or exhausted: surface to consumer.
        try:
            self._results.put(failure, timeout=30)
        except Full:
            self._results.put(failure)

    def _retry_delay(self, failure: JobFailure) -> float:
        if failure.retry_after is not None:
            return min(float(failure.retry_after), MAX_RETRY_DELAY_S)
        delay = 5 * (2 ** failure.attempt)
        return float(min(delay, MAX_RETRY_DELAY_S))

    def _note_429(self, retry_after: Optional[float]) -> None:
        self._consecutive_429 += 1
        if retry_after is not None:
            cooldown = float(retry_after)
        else:
            cooldown = min(
                DEFAULT_429_COOLDOWN_S * (2 ** (self._consecutive_429 - 1)),
                MAX_429_COOLDOWN_S,
            )
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + cooldown,
        )

    def _promote_delayed(self, now: float) -> None:
        while self._delayed and self._delayed[0].ready_at <= now:
            delayed = heapq.heappop(self._delayed)
            heapq.heappush(
                self._ready,
                ReadyJob(
                    sort_key=delayed.identity.sort_key(),
                    identity=delayed.identity,
                    attempt=delayed.attempt,
                ),
            )

    def _gate_wait(self, now: float) -> float:
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


def failure_from_job(failure: JobFailure) -> UnresolvedFailure:
    """Convert a scheduler permanent failure into a ledger entry."""

    return UnresolvedFailure(
        identity=failure.identity,
        category=failure.category,
        message=failure.message,
    )
