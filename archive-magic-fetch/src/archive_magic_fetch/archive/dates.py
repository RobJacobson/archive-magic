"""Date bounds and annual output partitions."""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import datetime

_CDX_FORMATS = {
    4: "%Y",
    6: "%Y%m",
    8: "%Y%m%d",
    14: "%Y%m%d%H%M%S",
}


def parse_date_bound(
    value: str | None,
    *,
    default: str,
    bound: str = "start",
) -> str:
    """Parse a date bound into a validated 14-digit UTC CDX timestamp."""

    raw = value or default
    text = raw.strip().replace("-", "")
    fmt = _CDX_FORMATS.get(len(text))
    if fmt is None or not text.isdigit():
        raise ValueError(f"invalid date bound: {raw!r}")
    try:
        parsed = datetime.strptime(text, fmt)
    except ValueError as error:
        raise ValueError(f"invalid date bound: {raw!r}") from error
    if bound == "end":
        # Fill unspecified fields to the last instant of this precision.
        if len(text) <= 4:
            parsed = parsed.replace(month=12, day=31)
        if len(text) <= 6:
            parsed = parsed.replace(
                day=calendar.monthrange(parsed.year, parsed.month)[1]
            )
        if len(text) <= 8:
            parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.strftime("%Y%m%d%H%M%S")


def validate_date_range(date_start: str, date_end: str) -> None:
    """Reject a reversed CDX date range."""

    if date_start > date_end:
        raise ValueError(f"start date {date_start} is after end date {date_end}")


def year_ranges(date_start: str, date_end: str) -> Iterator[tuple[int, str, str]]:
    """Yield each calendar year and its clipped CDX bounds."""

    for year in range(int(date_start[:4]), int(date_end[:4]) + 1):
        yield (
            year,
            max(date_start, f"{year:04d}0101000000"),
            min(date_end, f"{year:04d}1231235959"),
        )
