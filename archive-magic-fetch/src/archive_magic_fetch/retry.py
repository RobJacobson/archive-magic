"""Shared retry, Retry-After, and backpressure helpers."""

from __future__ import annotations

import errno
import time
from email.utils import mktime_tz, parsedate_tz
from typing import Optional


BACKPRESSURE_COOLDOWN_SECONDS = 60.0


def linear_backpressure_delay(
    attempt: int,
    retry_after: float | None = None,
) -> float:
    """Linear 60s, 120s, 180s, ... pause, never shorter than Retry-After."""

    return max(retry_after or 0.0, BACKPRESSURE_COOLDOWN_SECONDS * attempt)


def parse_retry_after(value: object) -> Optional[float]:
    """Return a positive delay in seconds from a Retry-After header value."""

    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        retry_date = parsedate_tz(value)
        if retry_date is None:
            return None
        seconds = float(mktime_tz(retry_date) - time.time())
    return seconds if seconds > 0 else None


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
