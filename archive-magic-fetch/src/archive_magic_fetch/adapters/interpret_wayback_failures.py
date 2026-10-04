"""Wayback failure interpretation, preflight markers, and replay links."""

from __future__ import annotations

import errno
import re

from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
)

from archive_magic_fetch.archive.identity import normalize_payload_digest
from archive_magic_fetch.contracts import FailureAdvice
from archive_magic_fetch.models import FailureCategory
from archive_magic_fetch.runtime.calculate_retry_delay import (
    BACKPRESSURE_COOLDOWN_SECONDS,
    iter_error_chain,
    retry_after_from_error,
)


class ExactMismatchError(MementoPlaybackError):
    """Returned memento is not the requested capture."""


class UnusablePlaybackError(MementoPlaybackError):
    """IA returned a non-content stub (Invalid URI or empty-vs-CDX mismatch)."""


_RETRYABLE_HTTP_STATUSES = frozenset({429, *range(500, 600)})


_STATUS_IN_MESSAGE = re.compile(r"\b([45]\d\d)\b")


def failure_advice(error: BaseException, attempt: int) -> FailureAdvice:
    category, retryable = classify_playback_error(error)
    pressure = backpressure_signal(error)
    kind = pressure[0] if pressure else None
    cooldown = (
        (*pressure, backpressure_source(kind))
        if pressure and pauses_playback_pool(kind)
        else None
    )
    return FailureAdvice(
        category,
        retryable,
        delay=retry_after_from_error(error) or float(5 * (2 ** (attempt - 1))),
        cooldown=cooldown,
        group="timeout" if kind == "timeout" else None,
        group_limit=2 if kind == "timeout" else None,
    )


def classify_playback_error(error: BaseException) -> tuple[FailureCategory, bool]:
    """Return (category, retryable) for a playback error."""

    if isinstance(error, ExactMismatchError):
        return FailureCategory.EXACT_MISMATCH, False
    if isinstance(error, UnusablePlaybackError):
        return FailureCategory.UNAVAILABLE, False
    if isinstance(error, (BlockedByRobotsError, BlockedSiteError)):
        return FailureCategory.BLOCKED, False
    name = type(error).__name__
    # IA can store permanently truncated payloads whose advertised length is
    # larger than the bytes available. requests commonly wraps IncompleteRead
    # in ChunkedEncodingError and wayback wraps that again, so inspect the
    # complete outer message before generic connection-error classification.
    if "IncompleteRead" in name or "Truncat" in name or "IncompleteRead" in str(error):
        return FailureCategory.TRUNCATED, False
    if "RateLimit" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    # Unwrap wayback's retry wrapper so connection/429 causes classify usefully.
    if "WaybackRetry" in name:
        nested = getattr(error, "cause", None)
        if isinstance(nested, BaseException):
            return classify_playback_error(nested)
        if isinstance(error.__cause__, BaseException):
            return classify_playback_error(error.__cause__)
        return FailureCategory.RETRY_EXHAUSTED, True
    if "Retryable" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    status = getattr(error, "status_code", None)
    if status is None:
        match = _STATUS_IN_MESSAGE.search(str(error))
        if match:
            status = int(match.group(1))
    if status in _RETRYABLE_HTTP_STATUSES:
        return FailureCategory.RETRY_EXHAUSTED, True
    if isinstance(error, MementoPlaybackError):
        return FailureCategory.UNAVAILABLE, False
    if "Timeout" in name or "Connection" in name or "Chunked" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    return FailureCategory.UNAVAILABLE, False


def pauses_playback_pool(kind: str | None) -> bool:
    """HTTP 429 and TCP refused pause all playback workers; timeouts do not."""

    return kind in ("http", "tcp")


def backpressure_signal(error: BaseException) -> tuple[str, float | None] | None:
    """Recognize IA backpressure through wrapper chains.

    HTTP 429 and TCP connection refused are explicit signals. Timeouts, HTTP
    504, and mid-request disconnects are classified as ``timeout`` so CDX can
    pause, but playback treats those as per-capture failures rather than a
    pool-wide rate limit.
    """

    http = False
    tcp = False
    timeout = False
    for candidate in iter_error_chain(error):
        name = type(candidate).__name__
        message = str(candidate).lower()
        response = getattr(candidate, "response", None)
        status = getattr(candidate, "status_code", None)
        response_status = getattr(response, "status_code", None)
        if (
            "RateLimit" in name
            or status == 429
            or response_status == 429
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
        if (
            status == 504
            or response_status == 504
            or "Timeout" in name
            or "RemoteDisconnected" in name
            or "timed out" in message
            or "read timeout" in message
            or "gateway timeout" in message
            or "wall-clock" in message
            or "connection aborted" in message
            or "remote end closed connection" in message
            or "504" in message
        ):
            timeout = True
    if http:
        return "http", retry_after_from_error(error)
    if tcp:
        return "tcp", BACKPRESSURE_COOLDOWN_SECONDS
    if timeout:
        return "timeout", retry_after_from_error(error)
    return None


def backpressure_source(kind: str) -> str:
    """Human-readable label for a ``backpressure_signal`` kind."""

    if kind == "http":
        return "HTTP 429"
    if kind == "tcp":
        return "TCP connection refused"
    if kind == "timeout":
        return "timeout/504"
    return kind


def is_invalid_uri_payload_digest(digest: object) -> bool:
    return normalize_payload_digest(digest) == INVALID_URI_PAYLOAD_DIGEST


def wayback_url(timestamp: str, original_url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


INVALID_URI_PAYLOAD_DIGEST = "sha1:L4XNRRGWXWKNIAJFQOC6D2OULYFIDDTC"
