"""Internet Archive CDX search and date-range helpers."""

from __future__ import annotations

import calendar
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from wayback import CdxRecord, WaybackClient

from .collection import normalize_domain
from .console import emit
from .identity import make_identity
from .models import ParsedCapture
from .playback import ArchiveMagicWaybackSession, classify_playback_error
from .retry import (
    BACKPRESSURE_COOLDOWN_SECONDS,
    backpressure_signal,
    iter_error_chain,
    retry_after_from_error,
)


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
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = emit,
) -> CdxResult:
    """Fetch and parse a CDX range through ``WaybackClient.search``.

    Fetch owns CDX retries. Wayback library retries stay disabled so a refused
    TCP connection or HTTP 429 pauses for 60s (or ``Retry-After``) instead of
    giving up after a few seconds of inner backoff. A failed query is retried
    from the start of the year range so the result is never a partial listing.
    """

    search_url, match_type = normalize_cdx_search(url_pattern)
    max_attempts = retries + 1
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        client = WaybackClient(
            session=ArchiveMagicWaybackSession(
                user_agent="archive-magic-fetch",
                retries=0,
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
                sorted(
                    map(_parsed_capture, records),
                    key=lambda item: item.identity.sort_key(),
                )
            )
            return CdxResult(captures, search_url, match_type)
        except Exception as error:  # noqa: BLE001 - network boundary
            last_error = error
            _, retryable = classify_playback_error(error)
            if retryable and attempt < max_attempts:
                delay = _cdx_retry_delay(error, attempt)
                report(_cdx_retry_message(error, delay, attempt, max_attempts))
                sleep(delay)
                continue
            break
        finally:
            client.close()
    assert last_error is not None
    detail = _unwrap_wayback_retry(last_error)
    raise RuntimeError(
        f"CDX query failed after {attempt} attempts: {detail}"
    ) from last_error


def _cdx_retry_delay(error: BaseException, attempt: int) -> float:
    backpressure = backpressure_signal(error)
    if backpressure is not None:
        _, retry_after = backpressure
        return retry_after or BACKPRESSURE_COOLDOWN_SECONDS
    return retry_after_from_error(error) or float(5 * (2 ** (attempt - 1)))


def _cdx_retry_message(
    error: BaseException,
    delay: float,
    attempt: int,
    max_attempts: int,
) -> str:
    backpressure = backpressure_signal(error)
    suffix = f"pausing {delay:g}s before attempt {attempt + 1}/{max_attempts}"
    if backpressure is None:
        return f"CDX query error during attempt {attempt}/{max_attempts}; {suffix} ({error})"
    kind, _retry_after = backpressure
    source = "HTTP 429" if kind == "http" else "TCP connection refused"
    return f"rate limit: {source} during CDX query; {suffix}"


def _unwrap_wayback_retry(error: BaseException) -> BaseException:
    for candidate in iter_error_chain(error):
        if "WaybackRetry" not in type(candidate).__name__:
            return candidate
    return error
