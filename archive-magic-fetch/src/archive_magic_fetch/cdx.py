"""Internet Archive CDX search and date-range helpers."""

from __future__ import annotations

import calendar
import json
import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path

from wayback import CdxRecord, WaybackClient

from .collection import (
    ArchiveLayout,
    exclusive_temp_path,
    normalize_domain,
    publish_file_atomically,
)
from .config import DEFAULT_CDX_PAGE_LIMIT, DEFAULT_CDX_WINDOW_DAYS
from .console import emit
from .identity import identity_from_dict, identity_to_dict, make_identity
from .models import ParsedCapture
from .playback import ArchiveMagicWaybackSession, classify_playback_error
from .retry import backpressure_signal, iter_error_chain, retry_after_from_error


DEFAULT_CDX_TIMEOUT_SECONDS = 300.0
CDX_SPLIT_FLOOR_DAYS = 7
CDX_TRANSIENT_ATTEMPTS = 3
CDX_TRANSIENT_DELAYS = (60.0, 120.0, 240.0)
CDX_RATE_LIMIT_ATTEMPTS = 10
CDX_RATE_LIMIT_INITIAL_DELAY = 60.0
CDX_RATE_LIMIT_MAX_DELAY = 600.0

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


def date_windows(
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


def format_cdx_window_label(date_start: str, date_end: str) -> str:
    """Render a CDX window as YYYY-MM-DD..YYYY-MM-DD for console logs."""

    def _day(value: str) -> str:
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"

    return f"{_day(date_start)}..{_day(date_end)}"


def format_cdx_index_scope(year: int, date_start: str, date_end: str) -> str:
    """Date range for 'fetching CDX index for …' lines."""

    if date_start[:8] == f"{year:04d}0101" and date_end[:8] == f"{year:04d}1231":
        return str(year)
    start = f"{date_start[:4]}-{date_start[4:6]}-{date_start[6:8]}"
    end = f"{date_end[:4]}-{date_end[4:6]}-{date_end[6:8]}"
    if start == end:
        return start
    return f"{start} to {end}"


def window_calendar_days(date_start: str, date_end: str) -> int:
    """Inclusive calendar-day span of a CDX timestamp window."""

    start = datetime.strptime(date_start[:8], "%Y%m%d")
    end = datetime.strptime(date_end[:8], "%Y%m%d")
    return (end - start).days + 1


def next_split_days(
    date_start: str,
    date_end: str,
    first_split_days: int,
) -> int | None:
    """Return the next split width, or None when the window is a hole."""

    width = window_calendar_days(date_start, date_end)
    if width > first_split_days:
        return first_split_days
    if width > CDX_SPLIT_FLOOR_DAYS:
        return CDX_SPLIT_FLOOR_DAYS
    return None


@dataclass(frozen=True)
class CdxResult:
    """Parsed captures and the CDX search that produced them."""

    captures: tuple[ParsedCapture, ...]
    search_url: str
    match_type: str | None


class CdxFailureKind(str, Enum):
    TRANSIENT = "transient"
    WALL_CLOCK = "wall_clock"
    RATE_LIMIT = "rate_limit"


@dataclass(frozen=True)
class CdxHole:
    date_start: str
    date_end: str
    kind: str
    message: str


@dataclass(frozen=True)
class CdxAcquisition:
    """Completed year listing, or the holes that still block publication."""

    captures: tuple[ParsedCapture, ...]
    search_url: str
    match_type: str | None
    holes: tuple[CdxHole, ...]
    fallback: str | None
    window_count: int


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


def _materialize_cdx_search(
    client: WaybackClient,
    *,
    search_url: str,
    match_type: str | None,
    date_start: str,
    date_end: str,
    limit: int,
) -> tuple[ParsedCapture, ...]:
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


def fetch_cdx(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    limit: int = DEFAULT_CDX_PAGE_LIMIT,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = emit,
) -> CdxResult:
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
    search_url, match_type = normalize_cdx_search(url_pattern)
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
            return CdxResult(captures, search_url, match_type)
        except Exception as error:  # noqa: BLE001 - network boundary
            last_error = error
            kind = classify_cdx_failure(error)
            if kind is None or kind is CdxFailureKind.WALL_CLOCK:
                break
            if kind is CdxFailureKind.RATE_LIMIT:
                seen_rate_limit = True
            if seen_rate_limit:
                kind = CdxFailureKind.RATE_LIMIT
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
            report(
                f"waiting {delay:g}s (attempt {attempt + 1}/{max_attempts})."
            )
            sleep(delay)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            client.close()
    assert last_error is not None
    detail = _unwrap_wayback_retry(last_error)
    raise RuntimeError(
        f"CDX query failed after {attempt} attempts: {detail}"
    ) from last_error


def classify_cdx_failure(error: BaseException) -> CdxFailureKind | None:
    """Classify a CDX error as transient, wall-clock, rate-limit, or permanent."""

    signal = backpressure_signal(error)
    if signal is not None:
        kind, _retry_after = signal
        if kind in ("http", "tcp"):
            return CdxFailureKind.RATE_LIMIT
        if kind == "timeout":
            if _is_wall_clock(error):
                return CdxFailureKind.WALL_CLOCK
            return CdxFailureKind.RATE_LIMIT
    _, retryable = classify_playback_error(error)
    if retryable:
        return CdxFailureKind.TRANSIENT
    return None


def cdx_should_split(error: BaseException) -> bool:
    """True when a failed window should split rather than become a hole.

    Only a wall-clock budget means the window itself is too expensive.
    HTTP 504 and dropped connections are IA overload; splitting them
    multiplies CDX traffic and turns a short outage into 429s.
    """

    return classify_cdx_failure(error) is CdxFailureKind.WALL_CLOCK


def acquire_year_cdx(
    *,
    layout: ArchiveLayout,
    year: int,
    url_pattern: str,
    date_start: str,
    date_end: str,
    cdx_window_days: int = DEFAULT_CDX_WINDOW_DAYS,
    cdx_page_limit: int = DEFAULT_CDX_PAGE_LIMIT,
    reset: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = emit,
) -> CdxAcquisition:
    """Query one year, splitting and checkpointing only the windows that fail."""

    path = layout.cdx_checkpoint(year)
    if reset:
        path.unlink(missing_ok=True)
    state = _load_checkpoint(path, url_pattern, cdx_page_limit, date_start, date_end)
    if state is None and path.is_file():
        report("ignoring saved CDX index progress (search settings changed).")
        path.unlink(missing_ok=True)
        state = _empty_checkpoint(url_pattern, cdx_page_limit, date_start, date_end)
    if state is None:
        state = _empty_checkpoint(url_pattern, cdx_page_limit, date_start, date_end)

    if state.holes:
        pending = list(state.holes)
        for hole in pending:
            _acquire_window(
                state,
                layout=layout,
                year=year,
                date_start=hole["date_start"],
                date_end=hole["date_end"],
                first_split_days=cdx_window_days,
                sleep=sleep,
                report=report,
            )
    elif state.completed:
        path.unlink(missing_ok=True)
        return _acquisition_from_state(state)
    else:
        report(
            "fetching CDX index for "
            f"{format_cdx_index_scope(year, date_start, date_end)}"
        )
        try:
            result = fetch_cdx(
                url_pattern=url_pattern,
                date_start=date_start,
                date_end=date_end,
                limit=cdx_page_limit,
                sleep=sleep,
                report=report,
            )
        except Exception as error:  # noqa: BLE001 - year CDX boundary
            kind = classify_cdx_failure(error)
            if kind is None:
                raise
            split_days = (
                next_split_days(date_start, date_end, cdx_window_days)
                if cdx_should_split(error)
                else None
            )
            if split_days is None:
                report(
                    "Could not get the CDX index for "
                    f"{format_cdx_index_scope(year, date_start, date_end)}."
                )
                state.set_hole(
                    date_start,
                    date_end,
                    kind=kind.value,
                    message=str(error),
                )
                _save_checkpoint(layout, year, state)
            else:
                state.fallback = "date_windows"
                children = list(date_windows(date_start, date_end, split_days))
                for child_start, child_end in children:
                    state.set_hole(
                        child_start,
                        child_end,
                        kind=kind.value,
                        message=str(error),
                    )
                _save_checkpoint(layout, year, state)
                report("CDX index took too long.")
                report(
                    f"fetching {split_days}-day ranges ({len(children)} ranges)."
                )
                for child_start, child_end in children:
                    _acquire_window(
                        state,
                        layout=layout,
                        year=year,
                        date_start=child_start,
                        date_end=child_end,
                        first_split_days=cdx_window_days,
                        sleep=sleep,
                        report=report,
                    )
        else:
            path.unlink(missing_ok=True)
            return CdxAcquisition(
                captures=result.captures,
                search_url=result.search_url,
                match_type=result.match_type,
                holes=(),
                fallback=None,
                window_count=1,
            )

    holes = tuple(
        CdxHole(
            date_start=item["date_start"],
            date_end=item["date_end"],
            kind=item["kind"],
            message=item["message"],
        )
        for item in state.holes
    )
    if holes:
        _save_checkpoint(layout, year, state)
    else:
        path.unlink(missing_ok=True)
    return _acquisition_from_state(state)


def _acquisition_from_state(state: "_CdxCheckpoint") -> CdxAcquisition:
    captures: list[ParsedCapture] = []
    for window in state.completed:
        for capture in window["captures"]:
            if isinstance(capture, ParsedCapture):
                captures.append(capture)
            else:
                captures.append(_capture_from_dict(capture))
    holes = tuple(
        CdxHole(
            date_start=item["date_start"],
            date_end=item["date_end"],
            kind=item["kind"],
            message=item["message"],
        )
        for item in state.holes
    )
    return CdxAcquisition(
        captures=tuple(captures),
        search_url=state.search_url,
        match_type=state.match_type,
        holes=holes,
        fallback=state.fallback,
        window_count=len(state.completed) + len(state.holes),
    )


def _acquire_window(
    state: "_CdxCheckpoint",
    *,
    layout: ArchiveLayout,
    year: int,
    date_start: str,
    date_end: str,
    first_split_days: int,
    sleep: Callable[[float], None],
    report: Callable[[str], None],
) -> None:
    if state.has_completed(date_start, date_end):
        return
    report(
        "fetching CDX index for "
        f"{format_cdx_index_scope(year, date_start, date_end)}"
    )
    try:
        result = fetch_cdx(
            url_pattern=state.url_pattern,
            date_start=date_start,
            date_end=date_end,
            limit=state.cdx_page_limit,
            sleep=sleep,
            report=report,
        )
    except Exception as error:  # noqa: BLE001 - isolate windows
        kind = classify_cdx_failure(error)
        if kind is None:
            raise
        split_days = (
            next_split_days(date_start, date_end, first_split_days)
            if cdx_should_split(error)
            else None
        )
        if split_days is None:
            report(
                "Could not get the CDX index for "
                f"{format_cdx_index_scope(year, date_start, date_end)}."
            )
            state.set_hole(
                date_start,
                date_end,
                kind=kind.value,
                message=str(error),
            )
            _save_checkpoint(layout, year, state)
            return
        state.fallback = "date_windows"
        children = list(date_windows(date_start, date_end, split_days))
        state.remove_bounds(date_start, date_end)
        for child_start, child_end in children:
            if not state.has_completed(child_start, child_end):
                state.set_hole(
                    child_start,
                    child_end,
                    kind=kind.value,
                    message=str(error),
                )
        _save_checkpoint(layout, year, state)
        report("CDX index took too long.")
        report(f"fetching {split_days}-day ranges ({len(children)} ranges).")
        for child_start, child_end in children:
            _acquire_window(
                state,
                layout=layout,
                year=year,
                date_start=child_start,
                date_end=child_end,
                first_split_days=first_split_days,
                sleep=sleep,
                report=report,
            )
        return
    state.search_url = result.search_url
    state.match_type = result.match_type
    state.add_completed(date_start, date_end, result.captures)
    _save_checkpoint(layout, year, state)


def _cdx_max_attempts(kind: CdxFailureKind) -> int:
    if kind is CdxFailureKind.RATE_LIMIT:
        return CDX_RATE_LIMIT_ATTEMPTS
    if kind is CdxFailureKind.TRANSIENT:
        return CDX_TRANSIENT_ATTEMPTS
    return 1


def _cdx_retry_delay(
    kind: CdxFailureKind,
    error: BaseException,
    attempt: int,
) -> float:
    retry_after = retry_after_from_error(error)
    if kind is CdxFailureKind.RATE_LIMIT:
        exponential = min(
            CDX_RATE_LIMIT_INITIAL_DELAY * (2 ** (attempt - 1)),
            CDX_RATE_LIMIT_MAX_DELAY,
        )
        return max(retry_after or 0.0, exponential)
    index = min(attempt - 1, len(CDX_TRANSIENT_DELAYS) - 1)
    delay = CDX_TRANSIENT_DELAYS[index]
    return max(retry_after or 0.0, delay)


_HTTP_STATUS = re.compile(r"\b(429|502|503|504)\b")


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
    return any("wall-clock" in str(candidate).lower() for candidate in iter_error_chain(error))


def _unwrap_wayback_retry(error: BaseException) -> BaseException:
    for candidate in iter_error_chain(error):
        if "WaybackRetry" not in type(candidate).__name__:
            return candidate
    return error


def _capture_to_dict(capture: ParsedCapture) -> dict[str, str]:
    payload = identity_to_dict(capture.identity)
    payload["mime"] = capture.mime
    return payload


def _capture_from_dict(data: dict[str, str]) -> ParsedCapture:
    mime = data["mime"]
    identity = identity_from_dict(
        {key: value for key, value in data.items() if key != "mime"}
    )
    return ParsedCapture(identity=identity, mime=mime)


@dataclass
class _CdxCheckpoint:
    url_pattern: str
    cdx_page_limit: int
    date_start: str
    date_end: str
    search_url: str = ""
    match_type: str | None = None
    completed: list[dict[str, object]] = field(default_factory=list)
    holes: list[dict[str, str]] = field(default_factory=list)
    fallback: str | None = None

    def has_completed(self, date_start: str, date_end: str) -> bool:
        return any(
            item["date_start"] == date_start and item["date_end"] == date_end
            for item in self.completed
        )

    def remove_bounds(self, date_start: str, date_end: str) -> None:
        self.completed = [
            item
            for item in self.completed
            if item["date_start"] != date_start or item["date_end"] != date_end
        ]
        self.holes = [
            item
            for item in self.holes
            if item["date_start"] != date_start or item["date_end"] != date_end
        ]

    def add_completed(
        self,
        date_start: str,
        date_end: str,
        captures: tuple[ParsedCapture, ...],
    ) -> None:
        self.remove_bounds(date_start, date_end)
        self.completed.append(
            {
                "date_start": date_start,
                "date_end": date_end,
                "captures": [_capture_to_dict(item) for item in captures],
            }
        )
        self.completed.sort(key=lambda item: (item["date_start"], item["date_end"]))

    def set_hole(
        self,
        date_start: str,
        date_end: str,
        *,
        kind: str,
        message: str,
    ) -> None:
        self.remove_bounds(date_start, date_end)
        self.holes.append(
            {
                "date_start": date_start,
                "date_end": date_end,
                "kind": kind,
                "message": message,
            }
        )
        self.holes.sort(key=lambda item: (item["date_start"], item["date_end"]))


def _empty_checkpoint(
    url_pattern: str,
    cdx_page_limit: int,
    date_start: str,
    date_end: str,
) -> _CdxCheckpoint:
    return _CdxCheckpoint(
        url_pattern=url_pattern,
        cdx_page_limit=cdx_page_limit,
        date_start=date_start,
        date_end=date_end,
    )


def _load_checkpoint(
    path: Path,
    url_pattern: str,
    cdx_page_limit: int,
    date_start: str,
    date_end: str,
) -> _CdxCheckpoint | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        payload.get("url_pattern") != url_pattern
        or payload.get("cdx_page_limit") != cdx_page_limit
    ):
        return None
    completed = []
    for item in payload.get("completed") or []:
        completed.append(
            {
                "date_start": item["date_start"],
                "date_end": item["date_end"],
                "captures": [
                    _capture_from_dict(capture) for capture in item.get("captures") or []
                ],
            }
        )
    # Store captures as ParsedCapture in memory; serialize on save.
    state = _CdxCheckpoint(
        url_pattern=url_pattern,
        cdx_page_limit=cdx_page_limit,
        date_start=date_start,
        date_end=date_end,
        search_url=payload.get("search_url") or "",
        match_type=payload.get("match_type"),
        fallback=payload.get("fallback"),
        holes=list(payload.get("holes") or []),
    )
    for item in completed:
        state.completed.append(
            {
                "date_start": item["date_start"],
                "date_end": item["date_end"],
                "captures": item["captures"],
            }
        )
    return state


def _save_checkpoint(layout: ArchiveLayout, year: int, state: _CdxCheckpoint) -> None:
    destination = layout.cdx_checkpoint(year)
    payload = {
        "url_pattern": state.url_pattern,
        "cdx_page_limit": state.cdx_page_limit,
        "date_start": state.date_start,
        "date_end": state.date_end,
        "search_url": state.search_url,
        "match_type": state.match_type,
        "fallback": state.fallback,
        "completed": [
            {
                "date_start": item["date_start"],
                "date_end": item["date_end"],
                "captures": [
                    _capture_to_dict(capture)
                    if isinstance(capture, ParsedCapture)
                    else capture
                    for capture in item["captures"]
                ],
            }
            for item in state.completed
        ],
        "holes": state.holes,
    }
    tmp = exclusive_temp_path(destination.parent, suffix=".json")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    publish_file_atomically(tmp, destination)
