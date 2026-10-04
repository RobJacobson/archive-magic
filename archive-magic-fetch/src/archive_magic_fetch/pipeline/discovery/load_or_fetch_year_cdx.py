"""Acquire complete Wayback listings with annual caching and bounded retries."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from wayback import CdxRecord, WaybackClient

from archive_magic_fetch.adapters.interpret_wayback_failures import (
    backpressure_signal,
    classify_playback_error,
)
from archive_magic_fetch.adapters.create_wayback_client import ArchiveMagicWaybackSession
from archive_magic_fetch.archive.dates import validate_date_range, year_ranges
from archive_magic_fetch.archive.identity import (
    identity_from_dict,
    identity_to_dict,
    make_identity,
)
from archive_magic_fetch.archive.layout import normalize_domain
from archive_magic_fetch.config.models import (
    DEFAULT_CDX_PAGE_LIMIT,
    DEFAULT_CDX_WINDOW_DAYS,
)
from archive_magic_fetch.models import CaptureListing, CaptureRef
from archive_magic_fetch.runtime.manage_archive_files import (
    exclusive_temp_path,
    publish_file_atomically,
)
from archive_magic_fetch.runtime.report_progress import emit
from archive_magic_fetch.runtime.calculate_retry_delay import iter_error_chain, retry_after_from_error

DEFAULT_CDX_TIMEOUT_SECONDS = 300.0

CDX_SPLIT_FLOOR_DAYS = 7

CDX_TRANSIENT_ATTEMPTS = 3

CDX_TRANSIENT_DELAYS = (60.0, 120.0, 240.0)

CDX_RATE_LIMIT_ATTEMPTS = 10

CDX_RATE_LIMIT_INITIAL_DELAY = 60.0

CDX_RATE_LIMIT_MAX_DELAY = 600.0

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


@dataclass(frozen=True)
class _CdxResult:
    """Parsed captures and the CDX search that produced them."""

    captures: tuple[CaptureRef, ...]
    search_url: str
    match_type: str | None


class _CdxFailureKind(str, Enum):
    TRANSIENT = "transient"
    WALL_CLOCK = "wall_clock"
    RATE_LIMIT = "rate_limit"


_HTTP_STATUS = re.compile(r"\b(429|502|503|504)\b")


def load_or_fetch_year_cdx(
    *,
    index_directory: Path,
    year: int,
    current_year: int,
    url_pattern: str,
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS,
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = emit,
) -> CaptureListing:
    """Return a full calendar-year listing, caching only completed past years.

    A final cache file is published only after every query page and fallback
    window succeeds. The caller holds the archive lock for this operation.
    Current-year results are always fetched and remain in memory.
    """

    if not 1 <= year <= current_year:
        raise ValueError(
            f"cannot acquire CDX year {year} (current year: {current_year})"
        )
    if cdx_window_days < 1 or cdx_page_limit < 1:
        raise ValueError("CDX window days and page limit must be positive")
    search_url, match_type = _normalize_cdx_search(url_pattern)
    path = index_directory / f"{year:04d}.cdx.json"
    historical = year < current_year
    if historical and (path.exists() or path.is_symlink()):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise ValueError("expected an array of captures")
            captures = tuple(_capture_from_dict(item) for item in payload)
            if any(int(item.identity.timestamp[:4]) != year for item in captures):
                raise ValueError(f"captures must belong to {year}")
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f"invalid CDX cache {path}: {error}") from error
        report(f"using cached CDX index for {year}: {path}")
        return _listing(captures, url_pattern, search_url, match_type, cdx_page_limit)

    def acquire(date_start: str, date_end: str) -> tuple[CaptureRef, ...]:
        report(
            "fetching CDX index for "
            f"{_format_cdx_index_scope(year, date_start, date_end)}"
        )
        try:
            return _fetch_cdx(
                url_pattern=url_pattern,
                date_start=date_start,
                date_end=date_end,
                limit=cdx_page_limit,
                sleep=sleep,
                report=report,
            ).captures
        except Exception as error:  # noqa: BLE001 - CDX network boundary
            split_days = (
                _next_split_days(date_start, date_end, cdx_window_days)
                if _cdx_should_split(error)
                else None
            )
            if split_days is None:
                raise
            report("CDX index took too long.")
            report(f"fetching {split_days}-day ranges.")
        # Fail immediately on any terminal child error. No partial result or
        # completed-window checkpoint survives a failed annual acquisition.
        captures = []
        for start, end in _date_windows(date_start, date_end, split_days):
            captures.extend(acquire(start, end))
        return tuple(captures)

    captures = acquire(f"{year:04d}0101000000", f"{year:04d}1231235959")
    if historical:
        tmp = exclusive_temp_path(path.parent, suffix=".cdx.json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump([_capture_to_dict(item) for item in captures], stream)
                stream.write("\n")
            publish_file_atomically(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        report(f"saved CDX index for {year}: {path}")
    return _listing(captures, url_pattern, search_url, match_type, cdx_page_limit)


def _normalize_cdx_search(url_pattern: str) -> tuple[str, str | None]:
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


def _date_windows(
    date_start: str,
    date_end: str,
    days: int,
) -> Iterator[tuple[str, str]]:
    """Yield inclusive CDX timestamp windows of up to ``days`` calendar days.

    Windows never cross a calendar-year boundary. The final window in a year
    (or overall) may be shorter than ``days``. Both bounds are 14-digit CDX
    timestamps. ``days`` must be at least 1.
    """

    if days < 1:
        raise ValueError(f"cdx window days must be positive, got {days}")
    validate_date_range(date_start, date_end)
    for _year, year_start, year_end in year_ranges(date_start, date_end):
        cursor = datetime.strptime(year_start[:8], "%Y%m%d")
        end_day = datetime.strptime(year_end[:8], "%Y%m%d")
        while cursor <= end_day:
            window_end_day = min(cursor + timedelta(days=days - 1), end_day)
            window_start = max(year_start, cursor.strftime("%Y%m%d") + "000000")
            window_end = min(year_end, window_end_day.strftime("%Y%m%d") + "235959")
            yield window_start, window_end
            cursor = window_end_day + timedelta(days=1)


def _format_cdx_window_label(date_start: str, date_end: str) -> str:
    """Render a CDX window as YYYY-MM-DD..YYYY-MM-DD for console logs."""

    def _day(value: str) -> str:
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"

    return f"{_day(date_start)}..{_day(date_end)}"


def _format_cdx_index_scope(year: int, date_start: str, date_end: str) -> str:
    """Date range for 'fetching CDX index for …' lines."""

    if date_start[:8] == f"{year:04d}0101" and date_end[:8] == f"{year:04d}1231":
        return str(year)
    start = f"{date_start[:4]}-{date_start[4:6]}-{date_start[6:8]}"
    end = f"{date_end[:4]}-{date_end[4:6]}-{date_end[6:8]}"
    if start == end:
        return start
    return f"{start} to {end}"


def _window_calendar_days(date_start: str, date_end: str) -> int:
    """Inclusive calendar-day span of a CDX timestamp window."""

    start = datetime.strptime(date_start[:8], "%Y%m%d")
    end = datetime.strptime(date_end[:8], "%Y%m%d")
    return (end - start).days + 1


def _next_split_days(
    date_start: str,
    date_end: str,
    first_split_days: int,
) -> int | None:
    """Return the next split width, or None at the smallest window."""

    width = _window_calendar_days(date_start, date_end)
    if width > first_split_days:
        return first_split_days
    if width > CDX_SPLIT_FLOOR_DAYS:
        return CDX_SPLIT_FLOOR_DAYS
    return None


def _parsed_capture(record: CdxRecord) -> CaptureRef:
    return CaptureRef(
        identity=make_identity(
            original_url=record.original,
            timestamp=record.timestamp.strftime("%Y%m%d%H%M%S"),
            status_token="-" if record.statuscode is None else str(record.statuscode),
            payload_digest=record.digest or "-",
            urlkey=record.urlkey,
        ),
        mime=record.mimetype or "-",
    )


def _materialize_cdx_search(
    client: WaybackClient,
    *,
    search_url: str,
    match_type: str | None,
    date_start: str,
    date_end: str,
    limit: int,
) -> tuple[CaptureRef, ...]:
    records = client.search(
        search_url,
        match_type=match_type,
        from_date=date_start,
        to_date=date_end,
        limit=limit,
        resolve_revisits=False,
        skip_malformed_results=True,
    )
    return tuple(
        sorted(
            map(_parsed_capture, records),
            key=lambda item: item.identity.sort_key(),
        )
    )


def _fetch_cdx(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    limit: int = DEFAULT_CDX_PAGE_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = emit,
) -> _CdxResult:
    """Fetch and parse a CDX range through ``WaybackClient.search``.

    Fetch owns CDX retries. Playback ``--retries`` does not apply. HTTP 504,
    read timeouts, 429s, and connection refused share one budget: ten attempts
    with ``Retry-After`` or exponential pauses from 60s capped at 10 minutes
    (about an hour of waiting). A wall-clock budget failure is not retried at
    the same scope. ``limit`` is the resumeKey page size per HTTP request.
    A failed query is retried from the start of the requested ``from``/``to``
    window so the result is never a partial listing.

    Each attempt has a wall-clock budget of ``DEFAULT_CDX_TIMEOUT_SECONDS``
    covering every resumeKey page. Socket read timeouts alone are not enough:
    IA can trickle bytes forever and reset the per-read timer.
    """

    if limit <= 0:
        raise ValueError(f"cdx page limit must be positive, got {limit}")
    search_url, match_type = _normalize_cdx_search(url_pattern)
    last_error: BaseException | None = None
    attempt = 0
    max_attempts = 1
    seen_rate_limit = False
    rate_limit_failures = 0
    while True:
        attempt += 1
        client = WaybackClient(
            session=ArchiveMagicWaybackSession(
                user_agent="archive-magic-fetch",
                retries=0,
                timeout=DEFAULT_CDX_TIMEOUT_SECONDS,
            )
        )
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(
                _materialize_cdx_search,
                client,
                search_url=search_url,
                match_type=match_type,
                date_start=date_start,
                date_end=date_end,
                limit=limit,
            )
            try:
                captures = future.result(timeout=DEFAULT_CDX_TIMEOUT_SECONDS)
            except FuturesTimeoutError as error:
                if future.done():
                    raise
                client.close()
                raise TimeoutError(
                    f"CDX query exceeded {DEFAULT_CDX_TIMEOUT_SECONDS:g}s "
                    "wall-clock budget"
                ) from error
            return _CdxResult(captures, search_url, match_type)
        except Exception as error:  # noqa: BLE001 - network boundary
            last_error = error
            kind = _classify_cdx_failure(error)
            if kind is None or kind is _CdxFailureKind.WALL_CLOCK:
                break
            if kind is _CdxFailureKind.RATE_LIMIT:
                seen_rate_limit = True
            if seen_rate_limit:
                kind = _CdxFailureKind.RATE_LIMIT
                rate_limit_failures += 1
                delay_attempt = rate_limit_failures
            else:
                delay_attempt = attempt
            max_attempts = _cdx_max_attempts(kind)
            if attempt >= max_attempts:
                report(_cdx_error_summary(error))
                break
            delay = _cdx_retry_delay(kind, error, delay_attempt)
            report(_cdx_error_summary(error))
            report(f"waiting {delay:g}s (attempt {attempt + 1}/{max_attempts}).")
            sleep(delay)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            client.close()
    assert last_error is not None
    detail = _unwrap_wayback_retry(last_error)
    raise RuntimeError(
        f"CDX query failed after {attempt} attempts: {detail}"
    ) from last_error


def _classify_cdx_failure(error: BaseException) -> _CdxFailureKind | None:
    """Classify a CDX error as transient, wall-clock, rate-limit, or permanent."""

    signal = backpressure_signal(error)
    if signal is not None:
        kind, _retry_after = signal
        if kind in ("http", "tcp"):
            return _CdxFailureKind.RATE_LIMIT
        if kind == "timeout":
            if _is_wall_clock(error):
                return _CdxFailureKind.WALL_CLOCK
            return _CdxFailureKind.RATE_LIMIT
    _, retryable = classify_playback_error(error)
    if retryable:
        return _CdxFailureKind.TRANSIENT
    return None


def _cdx_should_split(error: BaseException) -> bool:
    """True when a failed window should split rather than fail the year.

    Only a wall-clock budget means the window itself is too expensive.
    HTTP 504 and dropped connections are IA overload; splitting them
    multiplies CDX traffic and turns a short outage into 429s.
    """

    return _classify_cdx_failure(error) is _CdxFailureKind.WALL_CLOCK


def _cdx_max_attempts(kind: _CdxFailureKind) -> int:
    if kind is _CdxFailureKind.RATE_LIMIT:
        return CDX_RATE_LIMIT_ATTEMPTS
    if kind is _CdxFailureKind.TRANSIENT:
        return CDX_TRANSIENT_ATTEMPTS
    return 1


def _cdx_retry_delay(
    kind: _CdxFailureKind,
    error: BaseException,
    attempt: int,
) -> float:
    retry_after = retry_after_from_error(error)
    if kind is _CdxFailureKind.RATE_LIMIT:
        exponential = min(
            CDX_RATE_LIMIT_INITIAL_DELAY * (2 ** (attempt - 1)),
            CDX_RATE_LIMIT_MAX_DELAY,
        )
        return max(retry_after or 0.0, exponential)
    index = min(attempt - 1, len(CDX_TRANSIENT_DELAYS) - 1)
    delay = CDX_TRANSIENT_DELAYS[index]
    return max(retry_after or 0.0, delay)


def _http_status_from_error(error: BaseException) -> int | None:
    for candidate in iter_error_chain(error):
        for value in (
            getattr(candidate, "status_code", None),
            getattr(getattr(candidate, "response", None), "status_code", None),
        ):
            if isinstance(value, int) and 400 <= value <= 599:
                return value
        match = _HTTP_STATUS.search(str(candidate))
        if match:
            return int(match.group(1))
    return None


def _cdx_error_summary(error: BaseException) -> str:
    if _is_wall_clock(error):
        return "CDX index took too long."
    status = _http_status_from_error(error)
    if status == 504:
        return "Wayback timed out looking up the CDX index (HTTP 504)."
    if status == 429:
        return "Wayback returned HTTP 429 (too many requests)."
    if status == 503:
        return "Wayback is temporarily unavailable (HTTP 503)."
    if status == 502:
        return "Wayback returned a bad gateway (HTTP 502)."
    if status is not None:
        return f"Wayback returned HTTP {status} for the CDX index."
    signal = backpressure_signal(error)
    if signal is not None and signal[0] == "tcp":
        return "Could not connect to Wayback."
    if signal is not None and signal[0] == "timeout":
        return "Wayback timed out looking up the CDX index."
    return "CDX index request failed."


def _is_wall_clock(error: BaseException) -> bool:
    return any(
        "wall-clock" in str(candidate).lower() for candidate in iter_error_chain(error)
    )


def _unwrap_wayback_retry(error: BaseException) -> BaseException:
    for candidate in iter_error_chain(error):
        if "WaybackRetry" not in type(candidate).__name__:
            return candidate
    return error


def _capture_to_dict(capture: CaptureRef) -> dict[str, str]:
    payload = identity_to_dict(capture.identity)
    payload["mime"] = capture.mime
    return payload


def _capture_from_dict(data: object) -> CaptureRef:
    fields = (
        "urlkey",
        "original_url",
        "timestamp",
        "status_token",
        "payload_digest",
        "mime",
    )
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key] for key in fields
    ):
        raise ValueError("capture fields must be non-empty strings")
    timestamp = data["timestamp"]
    if len(timestamp) != 14 or not timestamp.isascii() or not timestamp.isdigit():
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    return CaptureRef(identity=identity_from_dict(data), mime=data["mime"])


def _listing(captures, url_pattern, search_url, match_type, page_limit):
    return CaptureListing(
        captures,
        {
            "url_pattern": url_pattern,
            "search_url": search_url,
            "match_type": match_type,
            "result_count": len(captures),
            "cdx_page_limit": page_limit,
        },
    )
