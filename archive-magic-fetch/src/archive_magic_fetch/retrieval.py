"""Wayback Memento retrieval and semantic WARC response construction."""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timezone
from http import HTTPStatus
from io import BytesIO
from typing import Callable, Mapping, Optional, Sequence

from requests.exceptions import ContentDecodingError, RequestException
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from wayback import Mode, WaybackClient, WaybackSession
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    RateLimitError,
    WaybackRetryError,
)

from .warc import timestamp_to_warc_date


DEFAULT_CONCURRENCY = 8

# Let the job-wide adaptive controller own sustained retry/backoff decisions.
# One in-session retry still absorbs a single dropped connection without
# multiplying independent exponential backoffs across every worker.
WORKER_SESSION_RETRIES = 1
MAX_THROTTLE_ATTEMPTS = 6
MAX_CONNECTION_BACKOFF_SECONDS = 30
RECOVERY_SUCCESSES_PER_STEP = 8
PREFETCH_MULTIPLIER = 2
_MISSING_HEADER = object()

_SKIPPABLE_PLAYBACK_ERRORS = (
    MementoPlaybackError,
    BlockedByRobotsError,
    BlockedSiteError,
    WaybackRetryError,
)

_PROGRESS_LOCK = threading.Lock()

_REPRESENTATION_HEADERS = {
    "content-digest",
    "content-encoding",
    "content-length",
    "content-md5",
    "content-range",
    "digest",
    "etag",
    "repr-digest",
    "transfer-encoding",
}


class MalformedContentEncodingError(MementoPlaybackError):
    """Wayback's declared content encoding does not match its response body."""


@dataclass(frozen=True)
class RetrievedMemento:
    """Semantic playback result reusable by WARC and loose-file writers."""

    body: bytes
    url: str
    capture_date: str
    source_uri: str
    status_code: int
    headers: tuple[tuple[str, str], ...]

    def to_warc_record(self):
        """Build a fresh WARC response record over the semantic body."""

        http_headers = StatusAndHeaders(
            _status_line(self.status_code),
            list(self.headers),
            protocol="HTTP/1.1",
        )
        builder = RecordBuilder(warc_version="1.0")
        return builder.create_warc_record(
            self.url,
            "response",
            payload=BytesIO(self.body),
            length=len(self.body),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Date": self.capture_date,
                "WARC-Source-URI": self.source_uri,
            },
        )


class RateLimitGate:
    """Adaptive, job-wide admission control for Wayback playback.

    The library's shared 8/s rate limiter spaces request starts. This gate
    independently controls how many requests may remain in flight, because
    server connection capacity and requests/second are different constraints.

    It starts conservatively, halves concurrency and applies one coordinated
    backoff per failure generation, then adds one slot after sustained success.
    """

    def __init__(self, max_concurrency: int = DEFAULT_CONCURRENCY) -> None:
        self._condition = threading.Condition()
        self._max_concurrency = max(1, max_concurrency)
        self._limit = min(2, self._max_concurrency)
        self._active = 0
        self._generation = 0
        self._failure_streak = 0
        self._success_streak = 0
        self._not_before = 0.0

    @property
    def generation(self) -> int:
        """Return the current coordinated-backoff generation."""

        with self._condition:
            return self._generation

    @property
    def concurrency_limit(self) -> int:
        """Return the current adaptive in-flight request limit."""

        with self._condition:
            return self._limit

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def configure(self, max_concurrency: int) -> None:
        """Set the job's ceiling before requests begin."""

        maximum = max(1, max_concurrency)
        with self._condition:
            if self._active:
                raise RuntimeError(
                    "cannot reconfigure retrieval concurrency while active"
                )
            self._max_concurrency = maximum
            self._limit = min(self._limit, maximum)
            self._condition.notify_all()

    def acquire(self) -> int:
        """Wait for backoff and capacity, then return a generation token."""

        with self._condition:
            while True:
                delay = self._not_before - time.monotonic()
                if delay > 0:
                    self._condition.wait(timeout=delay)
                    continue
                if self._active < self._limit:
                    self._active += 1
                    return self._generation
                self._condition.wait()

    def _release(self) -> None:
        if self._active <= 0:  # pragma: no cover - internal invariant
            raise RuntimeError("retrieval gate released without an active slot")
        self._active -= 1
        self._condition.notify_all()

    def after_success(self) -> None:
        """Release a slot and cautiously restore concurrency."""

        with self._condition:
            self._release()
            self._success_streak += 1
            if self._success_streak < RECOVERY_SUCCESSES_PER_STEP:
                return
            self._success_streak = 0
            self._failure_streak = 0
            if self._limit < self._max_concurrency:
                self._limit += 1
                self._condition.notify_all()

    def after_neutral(self) -> None:
        """Release a slot for a non-capacity-related playback outcome."""

        with self._condition:
            self._release()

    def after_throttle(
        self,
        start_generation: int,
        *,
        retry_after: Optional[float] = None,
    ) -> float:
        """Release a slot and coordinate adaptive backoff for one failure wave."""

        with self._condition:
            self._release()
            if self._generation != start_generation:
                return max(0.0, self._not_before - time.monotonic())

            self._generation += 1
            self._failure_streak += 1
            self._success_streak = 0
            self._limit = max(1, self._limit // 2)
            if retry_after is None:
                delay = min(
                    2 ** (self._failure_streak - 1),
                    MAX_CONNECTION_BACKOFF_SECONDS,
                )
            else:
                delay = max(0.0, retry_after)
            self._not_before = max(
                self._not_before,
                time.monotonic() + delay,
            )
            self._condition.notify_all()
            return delay


class RetrievalCache:
    """Fetch each distinct capture once and fan out to multiple writers."""

    def __init__(
        self,
        *,
        gate: Optional[RateLimitGate] = None,
        max_concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self._results: dict[tuple[object, ...], object] = {}
        self._preserved: set[tuple[object, ...]] = set()
        self._lock = threading.Lock()
        self._gate = (
            gate
            if gate is not None
            else RateLimitGate(max_concurrency=max_concurrency)
        )

    @property
    def gate(self) -> RateLimitGate:
        return self._gate

    @staticmethod
    def capture_key(capture) -> tuple[object, ...]:
        return (
            capture.urlkey,
            capture.original,
            capture.timestamp,
            capture.statuscode,
            capture.digest,
        )

    def get(self, capture) -> Optional[object]:
        """Return a cached result or exception without retrieving."""

        with self._lock:
            return self._results.get(self.capture_key(capture))

    def preserve(self, captures: Sequence) -> None:
        """Keep these results for a later output consumer."""

        with self._lock:
            self._preserved.update(
                self.capture_key(capture) for capture in captures
            )

    def discard(self, capture, *, force: bool = False) -> None:
        """Release a result unless it is reserved for a later consumer."""

        key = self.capture_key(capture)
        with self._lock:
            if force:
                self._preserved.discard(key)
            if force or key not in self._preserved:
                self._results.pop(key, None)

    def retrieve(self, client, capture) -> RetrievedMemento:
        """Return a cached memento or retrieve and remember it."""

        key = self.capture_key(capture)
        with self._lock:
            cached = self._results.get(key)
            if cached is not None:
                if isinstance(cached, BaseException):
                    raise cached
                return cached

        try:
            result = retrieve_memento(client, capture, gate=self._gate)
        except BaseException as error:
            with self._lock:
                existing = self._results.get(key)
                if existing is not None:
                    if isinstance(existing, BaseException):
                        raise existing
                    return existing
                self._results[key] = error
            raise

        with self._lock:
            existing = self._results.get(key)
            if existing is not None:
                if isinstance(existing, BaseException):
                    raise existing
                return existing
            self._results[key] = result
        return result


class MementoFetchPool:
    """Bounded worker pool: job queue for fetches, writer waits in order.

    Workers reuse one Wayback client per thread and report successful fetches
    as they complete. The writer calls ``wait`` for the next capture before
    reading the cache, independently preserving commit order.
    """

    def __init__(
        self,
        *,
        cache: RetrievalCache,
        client_factory: Callable[[], WaybackClient],
        max_workers: int = DEFAULT_CONCURRENCY,
        on_fetched: Optional[Callable[[object], None]] = None,
    ) -> None:
        self._cache = cache
        self._client_factory = client_factory
        self._on_fetched = on_fetched
        self._max_workers = max(1, max_workers)
        self._cache.gate.configure(self._max_workers)
        self._local = threading.local()
        self._clients: list = []
        self._clients_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)
        self._futures: dict[tuple[object, ...], Future] = {}

    def _thread_client(self):
        client = getattr(self._local, "client", None)
        if client is not None:
            return client
        client = self._client_factory()
        enter = getattr(client, "__enter__", None)
        if callable(enter):
            enter()
        self._local.client = client
        with self._clients_lock:
            self._clients.append(client)
        return client

    def _fetch(self, capture) -> None:
        try:
            self._cache.retrieve(self._thread_client(), capture)
        except _SKIPPABLE_PLAYBACK_ERRORS:
            return
        if self._on_fetched is not None:
            self._on_fetched(capture)

    def submit(self, captures: Sequence) -> None:
        """Enqueue distinct captures that are not already cached."""

        for capture in captures:
            key = RetrievalCache.capture_key(capture)
            if key in self._futures:
                continue
            if self._cache.get(capture) is not None:
                continue
            self._futures[key] = self._executor.submit(self._fetch, capture)

    def wait(self, capture) -> bool:
        """Wait for a submitted fetch and report whether a future existed."""

        future = self._futures.pop(
            RetrievalCache.capture_key(capture),
            None,
        )
        if future is None:
            return False
        future.result()
        return True

    def window(self, captures: Sequence) -> MementoFetchWindow:
        """Return a bounded lookahead window over ordered fetch work."""

        return MementoFetchWindow(
            self,
            captures,
            max_pending=max(1, self._max_workers * PREFETCH_MULTIPLIER),
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        for client in self._clients:
            exit_fn = getattr(client, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
                continue
            close = getattr(client, "close", None)
            if callable(close):
                close()


class MementoFetchWindow:
    """Keep only a bounded number of ordered fetch results outstanding."""

    def __init__(
        self,
        pool: MementoFetchPool,
        captures: Sequence,
        *,
        max_pending: int,
    ) -> None:
        self._pool = pool
        self._captures = iter(captures)
        self._max_pending = max_pending
        self._planned: dict[tuple[object, ...], int] = {}
        self._pending = 0
        self._prime()

    def _prime(self) -> None:
        batch = []
        while self._pending < self._max_pending:
            try:
                capture = next(self._captures)
            except StopIteration:
                break
            key = RetrievalCache.capture_key(capture)
            self._planned[key] = self._planned.get(key, 0) + 1
            batch.append(capture)
            self._pending += 1
        if batch:
            self._pool.submit(batch)

    def wait(self, capture) -> bool:
        """Wait for a planned capture, admit one more job, and report fetching."""

        key = RetrievalCache.capture_key(capture)
        count = self._planned.get(key, 0)
        if count == 0:
            return False
        if count == 1:
            del self._planned[key]
        else:
            self._planned[key] = count - 1
        fetched = self._pool.wait(capture)
        self._pending -= 1
        self._prime()
        return fetched


def print_fetched(capture) -> None:
    """Report network completion immediately, independent of write order."""

    timestamp = capture.timestamp.astimezone(timezone.utc).strftime(
        "%Y%m%d%H%M%S"
    )
    print_progress(f"Fetched {timestamp} {capture.original}")


def print_progress(message: str) -> None:
    """Print one complete progress line safely across worker threads."""

    with _PROGRESS_LOCK:
        print(message, flush=True)


def make_client_factory(user_agent: str) -> Callable[[], WaybackClient]:
    """Return a factory of Wayback clients that share default rate limits.

    Each call creates a fresh ``WaybackSession``. Unspecified rate limits use
    the library defaults, which are shared process-wide and thread-safe.
    Worker sessions use fewer retries so connection-refused storms surface to
    the job-wide gate quickly instead of retrying in parallel for ~64s each.
    """

    def factory() -> WaybackClient:
        return WaybackClient(
            session=WaybackSession(
                user_agent=user_agent,
                retries=WORKER_SESSION_RETRIES,
            )
        )

    return factory


def _retrieve_memento_with_retry(
    client,
    capture,
    *,
    gate: RateLimitGate,
) -> RetrievedMemento:
    """Retrieve and fully consume one Memento under adaptive admission."""

    started_at = time.monotonic()
    attempt_number = 0
    identity_retry = False
    identity_headers = None
    previous_accept_encoding = _MISSING_HEADER
    try:
        while attempt_number < MAX_THROTTLE_ATTEMPTS:
            attempt_number += 1
            generation = gate.acquire()
            try:
                memento = client.get_memento(
                    capture,
                    mode=Mode.original,
                    exact=True,
                    follow_redirects=False,
                )
                with memento:
                    payload = memento.content
                    headers = tuple(
                        _semantic_headers(memento.headers, len(payload))
                    )
                    result = RetrievedMemento(
                        body=payload,
                        url=memento.url,
                        capture_date=timestamp_to_warc_date(
                            memento.timestamp
                        ),
                        source_uri=memento.memento_url,
                        status_code=memento.status_code,
                        headers=headers,
                    )
            except ContentDecodingError as error:
                gate.after_neutral()
                if identity_retry:
                    raise MalformedContentEncodingError(
                        "Wayback response body still did not match its "
                        "Content-Encoding after retrying with "
                        f"Accept-Encoding: identity: {error}"
                    ) from error

                session = getattr(client, "session", None)
                identity_headers = getattr(session, "headers", None)
                if identity_headers is None:
                    raise MalformedContentEncodingError(
                        "Wayback response body did not match its "
                        "Content-Encoding and the client session cannot "
                        f"request identity encoding: {error}"
                    ) from error

                identity_retry = True
                attempt_number -= 1
                previous_accept_encoding = identity_headers.get(
                    "Accept-Encoding",
                    _MISSING_HEADER,
                )
                identity_headers["Accept-Encoding"] = "identity"
                reset = getattr(session, "reset", None)
                if callable(reset):
                    reset()
            except RateLimitError as error:
                gate.after_throttle(
                    generation,
                    retry_after=error.retry_after or 60,
                )
                if attempt_number == MAX_THROTTLE_ATTEMPTS:
                    raise
            except (WaybackRetryError, RequestException) as error:
                gate.after_throttle(generation)
                session = getattr(client, "session", None)
                reset = getattr(session, "reset", None)
                if callable(reset):
                    reset()
                if attempt_number == MAX_THROTTLE_ATTEMPTS:
                    if isinstance(error, RequestException):
                        raise WaybackRetryError(
                            attempt_number,
                            time.monotonic() - started_at,
                            error,
                        ) from error
                    raise
            except BaseException:
                gate.after_neutral()
                raise
            else:
                gate.after_success()
                return result
    finally:
        if identity_headers is not None:
            if previous_accept_encoding is _MISSING_HEADER:
                identity_headers.pop("Accept-Encoding", None)
            else:
                identity_headers["Accept-Encoding"] = (
                    previous_accept_encoding
                )

    raise RuntimeError("unreachable memento retry state")  # pragma: no cover


def _semantic_headers(
    headers: Mapping[str, str],
    payload_length: int,
) -> list[tuple[str, str]]:
    """Return historical headers consistent with the semantic payload."""

    semantic = [
        (name, value)
        for name, value in headers.items()
        if name.lower() not in _REPRESENTATION_HEADERS
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic


def _status_line(status_code: int) -> str:
    """Return a standard HTTP status line without inventing unknown reasons."""

    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def retrieve_memento(
    client,
    capture,
    *,
    gate: Optional[RateLimitGate] = None,
) -> RetrievedMemento:
    """Retrieve one Memento as reusable semantic body and metadata."""

    active_gate = gate if gate is not None else RateLimitGate()
    return _retrieve_memento_with_retry(
        client,
        capture,
        gate=active_gate,
    )


def retrieve_response(
    client,
    capture,
    *,
    cache: Optional[RetrievalCache] = None,
    gate: Optional[RateLimitGate] = None,
):
    """Retrieve one Memento and construct the semantic WARC response."""

    if cache is None:
        return retrieve_memento(client, capture, gate=gate).to_warc_record()
    return cache.retrieve(client, capture).to_warc_record()
