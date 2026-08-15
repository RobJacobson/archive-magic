"""Internet Archive CDX search and date-range helpers."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from wayback import CdxRecord, WaybackClient

from .collection import normalize_domain
from .identity import make_identity
from .models import ParsedCapture
from .playback import ArchiveMagicWaybackSession


# Compact CDX timestamps at year, month, day, or full second precision.
_CDX_FORMATS = {
    4: "%Y",
    6: "%Y%m",
    8: "%Y%m%d",
    14: "%Y%m%d%H%M%S",
}
# "*.example.org" (optional scheme and trailing /) is sugar for a CDX domain
# query. The host group is the hostname plus optional port; extra * or a
# leading dot is rejected so this stays a single-site wildcard.
_DOMAIN_WILDCARD = re.compile(
    r"""
    ^
    (?:[a-zA-Z][a-zA-Z0-9+.-]*://)?  # optional http:// or https://
    \*\.                              # one leading *.
    (?P<host>[^*/?#.][^*/?#]*)        # host[:port], no extra * or path
    /?                                # optional trailing slash
    $
    """,
    re.VERBOSE,
)


def normalize_cdx_search(url_pattern: str) -> tuple[str, str | None]:
    """Map url_pattern sugar to a CDX URL and match_type.

    ``*.example.org`` becomes ``("example.org", "domain")``. A trailing
    ``/*`` becomes a prefix match. Anything else is searched as written.
    """

    text = url_pattern.strip()
    wildcard = _DOMAIN_WILDCARD.fullmatch(text)
    if wildcard is not None:
        host, port = normalize_domain(wildcard["host"], allow_bare=True)
        return (host if port is None else f"{host}:{port}"), "domain"
    if text.endswith("/*"):
        return text.removesuffix("*"), "prefix"
    return text, None


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


@dataclass(frozen=True)
class CdxResult:
    """Parsed captures and the CDX search that produced them."""

    captures: tuple[ParsedCapture, ...]
    search_url: str
    match_type: str | None


def _parsed_capture(record: CdxRecord) -> ParsedCapture:
    return ParsedCapture(
        identity=make_identity(
            original_url=record.original,
            timestamp=record.timestamp.strftime("%Y%m%d%H%M%S"),
            status_token="-" if record.statuscode is None else str(record.statuscode),
            payload_digest=record.digest or "-",
            urlkey=record.urlkey,
        ),
        mime=record.mimetype or "-",
    )


def fetch_cdx(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    retries: int,
) -> CdxResult:
    """Fetch and parse a CDX range through ``WaybackClient.search``."""

    search_url, match_type = normalize_cdx_search(url_pattern)
    client = WaybackClient(
        session=ArchiveMagicWaybackSession(
            user_agent="archive-magic-fetch",
            retries=retries,
        )
    )
    try:
        records = client.search(
            search_url,
            match_type=match_type,
            from_date=date_start,
            to_date=date_end,
            resolve_revisits=False,
            skip_malformed_results=True,
        )
        captures = tuple(
            sorted(map(_parsed_capture, records), key=lambda item: item.identity.sort_key())
        )
    finally:
        client.close()
    return CdxResult(captures, search_url, match_type)
