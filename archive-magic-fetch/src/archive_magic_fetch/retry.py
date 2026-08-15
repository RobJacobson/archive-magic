"""Shared retry, Retry-After, and backpressure helpers."""

from __future__ import annotations

import errno
import time
from email.utils import mktime_tz, parsedate_tz
from typing import Optional


BACKPRESSURE_COOLDOWN_SECONDS = 60.0


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
        return "tcp", BACKPRESSURE_COOLDOWN_SECONDS
    return None
