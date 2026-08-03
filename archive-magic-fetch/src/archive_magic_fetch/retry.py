"""Application-owned retry policy for Internet Archive operations."""

from __future__ import annotations

import time
from dataclasses import dataclass
from email.utils import mktime_tz, parsedate_tz
from typing import Optional

from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ContentDecodingError,
    SSLError,
    Timeout,
)
from wayback import WaybackSession
from wayback.exceptions import RateLimitError, WaybackRetryError


DEFAULT_RETRIES = 8
MAX_SLEEP_CHUNK_SECONDS = 3600
RETRYABLE_HTTP_STATUSES = frozenset(
    {413, 421, 429, 500, 502, 503, 504, 599}
)


def short_cause(cause: Optional[BaseException]) -> str:
    """Return one short, single-line cause suitable for console output."""

    if cause is None:
        return ""
    text = " ".join(str(cause).split())
    if not text:
        return type(cause).__name__
    lower = text.lower()
    for needle in (
        "incorrect header check",
        "incorrect gzip header",
        "crc check failed",
        "invalid stored block lengths",
        "connection reset",
        "read timed out",
        "timed out",
    ):
        if needle in lower:
            return needle
    if len(text) > 72:
        return text[:69] + "..."
    return text


@dataclass(frozen=True)
class RetryDecision:
    """Why an Internet Archive operation should be retried."""

    cause: BaseException
    retry_after: Optional[float] = None


class RetryableWaybackResponseError(Exception):
    """A non-Memento Internet Archive response that may recover."""

    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        retry_after: Optional[float],
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.retry_after = retry_after
        super().__init__(
            f"Internet Archive returned retryable HTTP {status_code} "
            f"for {url}"
        )


class RetryExhaustedError(Exception):
    """An operation remained transiently unavailable after every attempt."""

    def __init__(
        self,
        *,
        attempts: int,
        elapsed_seconds: float,
        cause: BaseException,
    ) -> None:
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.cause = cause
        noun = "attempt" if attempts == 1 else "attempts"
        super().__init__(
            f"Wayback request failed after {attempts} {noun} over "
            f"{elapsed_seconds:.1f}s: {cause}"
        )


def _parse_retry_after(value: object) -> Optional[float]:
    """Parse an HTTP Retry-After value as a non-negative delay."""

    if not isinstance(value, str):
        return None
    try:
        return float(max(0, int(value)))
    except ValueError:
        retry_date = parsedate_tz(value)
        if retry_date is None:
            return None
        return float(max(0, mktime_tz(retry_date) - int(time.time())))


class ArchiveMagicWaybackSession(WaybackSession):
    """Wayback session that exposes retryable responses without retrying."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["retries"] = 0
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        kwargs["stream"] = True
        response = super().send(request, **kwargs)
        if (
            response.status_code in RETRYABLE_HTTP_STATUSES
            and "Memento-Datetime" not in response.headers
        ):
            error = RetryableWaybackResponseError(
                status_code=response.status_code,
                url=response.url,
                retry_after=_parse_retry_after(
                    response.headers.get("Retry-After")
                ),
            )
            response.close()
            raise error
        return response


def retry_decision(error: BaseException) -> Optional[RetryDecision]:
    """Return retry metadata for one transient failure."""

    if isinstance(error, RateLimitError):
        return RetryDecision(error, error.retry_after)
    if isinstance(error, RetryableWaybackResponseError):
        return RetryDecision(error, error.retry_after)
    if isinstance(error, WaybackRetryError):
        return retry_decision(error.cause)
    if isinstance(error, (ContentDecodingError, SSLError)):
        return None
    if isinstance(
        error,
        (ChunkedEncodingError, ConnectionError, Timeout),
    ):
        return RetryDecision(error)
    return None


def retry_delay_seconds(
    retry_number: int,
    *,
    retry_after: Optional[float] = None,
) -> int | float:
    """Return the deterministic delay before a numbered retry."""

    if retry_number < 1:
        raise ValueError("retry number must be at least 1")
    delay: int | float = 5 * (2**retry_number)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


def sleep_seconds(seconds: int | float) -> None:
    """Sleep a possibly enormous duration without platform overflow."""

    if seconds < 0:
        raise ValueError("sleep duration cannot be negative")
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, MAX_SLEEP_CHUNK_SECONDS)
        time.sleep(chunk)
        remaining -= chunk


def format_seconds(seconds: int | float) -> str:
    """Format a retry delay without coercing enormous integers to floats."""

    if isinstance(seconds, int):
        return f"{seconds:,}"
    return f"{seconds:g}"
