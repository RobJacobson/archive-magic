"""Shared Retry-After parsing for CDX and playback network boundaries."""

from __future__ import annotations

import time
from email.utils import mktime_tz, parsedate_tz
from typing import Optional


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
