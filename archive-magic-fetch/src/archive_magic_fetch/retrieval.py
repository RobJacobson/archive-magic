"""Wayback Memento retrieval and semantic WARC response construction."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.client import IncompleteRead as HttpIncompleteRead
from io import BytesIO
from typing import Callable, Mapping, Optional

from requests.exceptions import ContentDecodingError, RequestException
from urllib3.exceptions import IncompleteRead as Urllib3IncompleteRead
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from wayback import Mode, WaybackClient, WaybackSession
from wayback.exceptions import (
    MementoPlaybackError,
    RateLimitError,
    WaybackRetryError,
)

from .console import print_progress
from .warc import timestamp_to_warc_date


DEFAULT_CONCURRENCY = 8

# One in-session retry still absorbs a single dropped connection. Sustained
# failures use the bounded per-capture retry policy below.
WORKER_SESSION_RETRIES = 1
MAX_RETRIEVAL_ATTEMPTS = 6
REPEATED_TRUNCATION_ATTEMPTS = 3
MAX_CONNECTION_BACKOFF_SECONDS = 30
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 60
_MISSING_HEADER = object()

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

    def __init__(
        self,
        encoding: Optional[str] = None,
        *,
        identity_retry_failed: bool = True,
    ) -> None:
        self.encoding = encoding
        self.identity_retry_failed = identity_retry_failed
        if encoding:
            detail = (
                f"Content-Encoding declares {encoding}, but the body could "
                "not be decoded"
            )
        else:
            detail = (
                "the body could not be decoded according to its declared "
                "Content-Encoding"
            )
        if identity_retry_failed:
            retry_detail = (
                "retrying with Accept-Encoding: identity also failed"
            )
        else:
            retry_detail = (
                "the client session could not retry with "
                "Accept-Encoding: identity"
            )
        super().__init__(
            f"invalid Wayback replay response: {detail}; {retry_detail}"
        )


class TruncatedWaybackResponseError(MementoPlaybackError):
    """Wayback repeatedly stopped at the same incomplete response boundary."""

    def __init__(
        self,
        *,
        received_bytes: int,
        expected_bytes: int,
        attempts: int,
        elapsed_seconds: float,
    ) -> None:
        self.received_bytes = received_bytes
        self.expected_bytes = expected_bytes
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            "truncated Wayback response after "
            f"{attempts} attempts over {elapsed_seconds:.1f}s "
            f"(received {received_bytes:,} of {expected_bytes:,} bytes)"
        )


def format_playback_failure(error: Exception) -> str:
    """Return one concise user-facing playback failure reason."""

    if isinstance(
        error,
        (MalformedContentEncodingError, TruncatedWaybackResponseError),
    ):
        return str(error)
    if isinstance(error, WaybackRetryError):
        elapsed = (
            f"{float(error.time):.1f}s"
            if isinstance(error.time, (int, float))
            else "an unknown duration"
        )
        attempts = "attempt" if error.retries == 1 else "attempts"
        return (
            f"Wayback request failed after {error.retries} {attempts} over "
            f"{elapsed}: {error.cause}"
        )
    return str(error) or type(error).__name__


def format_playback_failure_summary(
    total: int,
    *,
    invalid_content_encoding: int,
    truncated_response: int,
) -> str:
    """Format a total with complete category detail when useful."""

    noun = "failure" if total == 1 else "failures"
    base = f"{total} playback {noun}"
    categorized = invalid_content_encoding + truncated_response
    if total == 0 or categorized == 0:
        return base

    categories = []
    if invalid_content_encoding:
        categories.append(
            f"{invalid_content_encoding} invalid content encoding"
        )
    if truncated_response:
        categories.append(f"{truncated_response} truncated response")
    other = total - categorized
    if other > 0:
        categories.append(f"{other} other")
    return f"{base} ({', '.join(categories)})"


def _content_encoding(memento) -> Optional[str]:
    """Return the historical content encoding involved in a decode failure."""

    headers = getattr(memento, "headers", None)
    if headers is None:
        return None
    value = headers.get("Content-Encoding")
    if value is None:
        return None
    encoding = str(value).strip()
    return encoding or None


def _incomplete_read_boundary(
    error: BaseException,
) -> Optional[tuple[int, int]]:
    """Find a structured IncompleteRead boundary in nested request errors."""

    pending: list[object] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)

        if isinstance(
            current,
            (HttpIncompleteRead, Urllib3IncompleteRead),
        ):
            partial = current.partial
            expected = current.expected
            received = (
                len(partial)
                if isinstance(partial, (bytes, bytearray))
                else partial
            )
            if (
                isinstance(received, int)
                and isinstance(expected, int)
                and received >= 0
                and expected >= 0
            ):
                return received, received + expected
            return None

        if isinstance(current, BaseException):
            pending.extend(current.args)
            for attribute in ("cause", "__cause__", "__context__"):
                nested = getattr(current, attribute, None)
                if nested is not None:
                    pending.append(nested)
        elif isinstance(current, (tuple, list)):
            pending.extend(current)
    return None


@dataclass(frozen=True)
class RetrievedMemento:
    """Semantic playback result reusable by WARC and loose-file writers."""

    body: bytes
    url: str
    capture_date: str
    source_uri: str
    status_code: int
    headers: tuple[tuple[str, str], ...]

    def to_warc_record(self, *, target_url: Optional[str] = None):
        """Build a fresh WARC response record over the semantic body."""

        http_headers = StatusAndHeaders(
            _status_line(self.status_code),
            list(self.headers),
            protocol="HTTP/1.1",
        )
        builder = RecordBuilder(warc_version="1.0")
        return builder.create_warc_record(
            target_url or self.url,
            "response",
            payload=BytesIO(self.body),
            length=len(self.body),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Date": self.capture_date,
                "WARC-Source-URI": self.source_uri,
            },
        )


class RateLimitCooldown:
    """Share a Wayback HTTP 429 pause across all retrieval workers."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._not_before = 0.0

    def wait(self) -> None:
        """Wait until the latest shared rate-limit pause expires."""
        with self._condition:
            while True:
                delay = self._not_before - time.monotonic()
                if delay <= 0:
                    return
                self._condition.wait(timeout=delay)

    def pause(self, seconds: float) -> bool:
        """Extend the shared pause and return whether this starts a new pause."""
        duration = max(0.0, seconds)
        with self._condition:
            now = time.monotonic()
            starts_pause = self._not_before <= now
            self._not_before = max(self._not_before, now + duration)
            self._condition.notify_all()
            return starts_pause


def _transient_backoff_seconds(failure_number: int) -> float:
    """Return bounded exponential backoff with full jitter."""
    ceiling = min(
        2 ** max(0, failure_number - 1),
        MAX_CONNECTION_BACKOFF_SECONDS,
    )
    return random.uniform(0, ceiling)


def make_client_factory(user_agent: str) -> Callable[[], WaybackClient]:
    """Return a factory of Wayback clients that share default rate limits.

    Each call creates a fresh ``WaybackSession``. Unspecified rate limits use
    the library defaults, which are shared process-wide and thread-safe.
    Worker sessions use fewer retries so connection-refused storms surface to
    Fetch's bounded retry loop instead of retrying in parallel for ~64s each.
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
    cooldown: RateLimitCooldown,
) -> RetrievedMemento:
    """Retrieve and fully consume one Memento with bounded transport retries."""

    started_at = time.monotonic()
    attempt_number = 0
    transient_failures = 0
    identity_retry = False
    identity_headers = None
    previous_accept_encoding = _MISSING_HEADER
    previous_truncation = None
    repeated_truncations = 0
    try:
        while attempt_number < MAX_RETRIEVAL_ATTEMPTS:
            cooldown.wait()
            attempt_number += 1
            memento = None
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
                if identity_retry:
                    raise MalformedContentEncodingError(
                        _content_encoding(memento)
                    ) from error

                session = getattr(client, "session", None)
                identity_headers = getattr(session, "headers", None)
                if identity_headers is None:
                    raise MalformedContentEncodingError(
                        _content_encoding(memento),
                        identity_retry_failed=False,
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
                previous_truncation = None
                repeated_truncations = 0
                transient_failures = 0
                if attempt_number == MAX_RETRIEVAL_ATTEMPTS:
                    raise WaybackRetryError(
                        attempt_number,
                        time.monotonic() - started_at,
                        error,
                    ) from error
                delay = (
                    error.retry_after
                    or DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
                )
                if cooldown.pause(delay):
                    print_progress(
                        "Rate limited by Internet Archive during playback; "
                        f"pausing all downloads for {delay:g}s before "
                        "retrying..."
                    )
            except (WaybackRetryError, RequestException) as error:
                truncation = _incomplete_read_boundary(error)
                if truncation is not None:
                    if truncation == previous_truncation:
                        repeated_truncations += 1
                    else:
                        previous_truncation = truncation
                        repeated_truncations = 1
                else:
                    previous_truncation = None
                    repeated_truncations = 0

                session = getattr(client, "session", None)
                reset = getattr(session, "reset", None)
                if callable(reset):
                    reset()

                repeated_boundary = (
                    truncation is not None
                    and repeated_truncations
                    >= REPEATED_TRUNCATION_ATTEMPTS
                )
                if repeated_boundary:
                    received, expected = truncation
                    raise TruncatedWaybackResponseError(
                        received_bytes=received,
                        expected_bytes=expected,
                        attempts=attempt_number,
                        elapsed_seconds=time.monotonic() - started_at,
                    ) from error

                transient_failures += 1
                if attempt_number == MAX_RETRIEVAL_ATTEMPTS:
                    if truncation is not None:
                        received, expected = truncation
                        raise TruncatedWaybackResponseError(
                            received_bytes=received,
                            expected_bytes=expected,
                            attempts=attempt_number,
                            elapsed_seconds=time.monotonic() - started_at,
                        ) from error
                    if isinstance(error, RequestException):
                        raise WaybackRetryError(
                            attempt_number,
                            time.monotonic() - started_at,
                            error,
                        ) from error
                    raise
                time.sleep(
                    _transient_backoff_seconds(transient_failures)
                )
            else:
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
    cooldown: Optional[RateLimitCooldown] = None,
) -> RetrievedMemento:
    """Retrieve one Memento as reusable semantic body and metadata."""

    active_cooldown = (
        cooldown if cooldown is not None else RateLimitCooldown()
    )
    return _retrieve_memento_with_retry(
        client,
        capture,
        cooldown=active_cooldown,
    )


def retrieve_response(
    client,
    capture,
    *,
    cooldown: Optional[RateLimitCooldown] = None,
):
    """Retrieve one Memento and construct the semantic WARC response."""

    return retrieve_memento(
        client,
        capture,
        cooldown=cooldown,
    ).to_warc_record()
