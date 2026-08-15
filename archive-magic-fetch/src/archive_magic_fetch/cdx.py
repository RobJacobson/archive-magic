"""Internet Archive CDX search and date-range helpers."""

from __future__ import annotations

import calendar
import importlib.metadata
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

from wayback import WaybackClient

from .collection import ArchiveLayout, ensure_collection_dirs, normalize_domain
from .identity import current_run_id, make_identity
from .models import ParsedCapture
from .playback import ArchiveMagicWaybackSession


_DATE_BOUND = re.compile(r"^\d{4,14}$")
_HYPHENATED_DATE = re.compile(r"^\d{4}(?:-\d{2}){0,2}$")


def normalize_cdx_search(url_pattern: str) -> tuple[str, Optional[str]]:
    """Rewrite URL sugar into an explicit CDX match type when needed."""

    if not isinstance(url_pattern, str) or not url_pattern:
        raise ValueError("URL pattern must be a non-empty string")
    text = url_pattern.strip()
    domain_target = _domain_wildcard_target(text)
    if domain_target is not None:
        return domain_target, "domain"
    if text.endswith("/*"):
        return text.removesuffix("*"), "prefix"
    return text, None


def _domain_wildcard_target(url_pattern: str) -> Optional[str]:
    remainder = url_pattern
    if "://" in remainder:
        remainder = remainder.split("://", 1)[1]
    if remainder.startswith("//"):
        remainder = remainder[2:]
    if not remainder.startswith("*."):
        return None
    host_part = remainder[2:].split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host_part or "*" in host_part or host_part.startswith("."):
        raise ValueError(
            f"URL pattern must use a single leading *. on the host: {url_pattern}"
        )
    host, port = normalize_domain(host_part, allow_bare=True)
    return host if port is None else f"{host}:{port}"


def _validate_calendar_date(year: int, month: int, day: int) -> None:
    if year < 1991 or year > 9999:
        raise ValueError(f"invalid date year: {year}")
    if month < 1 or month > 12:
        raise ValueError(f"invalid date month: {month}")
    last_day = calendar.monthrange(year, month)[1]
    if day < 1 or day > last_day:
        raise ValueError(f"invalid date day: {year:04d}-{month:02d}-{day:02d}")


def parse_date_bound(
    value: Optional[str],
    *,
    default: str,
    bound: str = "start",
) -> str:
    """Parse a date bound into a validated 14-digit UTC CDX timestamp."""

    raw = default if value is None or value == "" else value
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("date bound must be a non-empty string")
    text = raw.strip()
    if "-" in text:
        if not _HYPHENATED_DATE.fullmatch(text):
            raise ValueError(f"invalid date bound: {raw!r}")
        text = text.replace("-", "")
    if not _DATE_BOUND.fullmatch(text):
        raise ValueError(f"invalid date bound: {raw!r}")
    if len(text) not in {4, 6, 8, 10, 12, 14}:
        raise ValueError(f"invalid date bound length: {raw!r}")

    year = int(text[0:4])
    if len(text) == 4:
        _validate_calendar_date(year, 1, 1)
        return f"{year:04d}{'1231235959' if bound == 'end' else '0101000000'}"

    month = int(text[4:6])
    if len(text) == 6:
        _validate_calendar_date(year, month, 1)
        if bound == "end":
            last = calendar.monthrange(year, month)[1]
            return f"{year:04d}{month:02d}{last:02d}235959"
        return f"{year:04d}{month:02d}01000000"

    day = int(text[6:8])
    _validate_calendar_date(year, month, day)
    if len(text) == 8:
        return f"{text}{'235959' if bound == 'end' else '000000'}"

    hour = int(text[8:10])
    if hour > 23:
        raise ValueError(f"invalid date hour: {raw!r}")
    if len(text) == 10:
        return f"{text}{'5959' if bound == 'end' else '0000'}"

    minute = int(text[10:12])
    if minute > 59:
        raise ValueError(f"invalid date minute: {raw!r}")
    if len(text) == 12:
        return f"{text}{'59' if bound == 'end' else '00'}"

    if int(text[12:14]) > 59:
        raise ValueError(f"invalid date second: {raw!r}")
    return text


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


def _wayback_version() -> str:
    try:
        return importlib.metadata.version("wayback")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class CdxResult:
    """Parsed captures and concise provenance for one CDX query."""

    captures: tuple[ParsedCapture, ...]
    query: dict[str, object]


def make_cdx_client(retries: int) -> WaybackClient:
    """Build a CDX client using the archive's configured retry policy."""

    session = ArchiveMagicWaybackSession(
        user_agent="archive-magic-fetch",
        retries=retries,
    )
    return WaybackClient(session=session)


def fetch_cdx(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    retries: int,
    client: WaybackClient | None = None,
) -> CdxResult:
    """Fetch and parse a CDX range through ``WaybackClient.search``."""

    search_url, match_type = normalize_cdx_search(url_pattern)
    owned_client = client is None
    client = client or make_cdx_client(retries)
    started = time.monotonic()
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
            sorted(
                (
                    ParsedCapture(
                        identity=make_identity(
                            original_url=record.original,
                            timestamp=record.timestamp.strftime("%Y%m%d%H%M%S"),
                            status_token=(
                                "-" if record.statuscode is None else str(record.statuscode)
                            ),
                            payload_digest=record.digest or "-",
                            urlkey=record.urlkey,
                        ),
                        mime=record.mimetype or "-",
                    )
                    for record in records
                ),
                key=lambda item: item.identity.sort_key(),
            )
        )
    finally:
        if owned_client:
            client.close()

    return CdxResult(
        captures=captures,
        query={
            "url_pattern": url_pattern,
            "search_url": search_url,
            "match_type": match_type,
            "from": date_start,
            "to": date_end,
            "client": "wayback",
            "wayback_version": _wayback_version(),
            "result_count": len(captures),
            "duration_s": round(time.monotonic() - started, 3),
        },
    )


def init_run_id(layout: ArchiveLayout, run_id: str | None = None) -> str:
    """Allocate one invocation ID shared by every selected collection."""

    ensure_collection_dirs(layout)
    if run_id is not None:
        layout.validate_run_id(run_id)
        if any(layout.captures_root.glob(f"*/runs/{run_id}")):
            raise FileExistsError(f"run ID already exists: {run_id}")
        return run_id

    for attempt in range(1000):
        candidate = (
            current_run_id()
            if attempt == 0
            else f"{current_run_id()}-{attempt:02d}"
        )
        if not any(layout.captures_root.glob(f"*/runs/{candidate}")):
            return candidate
    raise RuntimeError("unable to allocate a unique run source directory")
