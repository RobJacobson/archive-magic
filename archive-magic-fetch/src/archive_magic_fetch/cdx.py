"""Annual CDX acquisition, raw persistence, and row parsing."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import mktime_tz, parsedate_tz
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlencode

import requests
from wayback import WaybackClient, WaybackSession

from .collection import CollectionLayout, ensure_collection_dirs, exclusive_temp_path, publish_file_atomically
from .models import (
    CDX_PAGE_LIMIT,
    DEFAULT_DATE_START as DEFAULT_DATE_START,
    USER_AGENT,
    CaptureIdentity,
    FailureCategory,
    ParsedCapture,
    UnresolvedFailure,
    cdx_payload_digest_token,
    cdx_status_token,
    current_run_id,
    is_redirect_status_token,
    make_identity,
    normalize_payload_digest,
    timestamp_year,
)
from .collection import normalize_domain


# Standard CDX field order used by IA and wayback.
_CDX_FIELDS = (
    "urlkey",
    "timestamp",
    "original",
    "mimetype",
    "statuscode",
    "digest",
    "length",
)
_CDX_TIMESTAMP = re.compile(r"^\d{1,14}$")


class ArchiveMagicWaybackSession(WaybackSession):
    """Wayback session with library-owned retries disabled."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["retries"] = 0
        super().__init__(*args, **kwargs)


def make_client() -> WaybackClient:
    """Return a Wayback client using the shared process rate limits."""

    return WaybackClient(session=ArchiveMagicWaybackSession(user_agent=USER_AGENT))


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
    return url_pattern, None


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
    if port is None:
        return host
    return f"{host}:{port}"


def parse_date_bound(value: Optional[str], *, default: str) -> str:
    """Parse a CDX date bound into a 14-digit UTC timestamp."""

    if value is None or value == "":
        return default
    text = value.strip()
    if not re.fullmatch(r"\d{4,14}", text):
        raise ValueError(f"invalid date bound: {value!r}")
    if len(text) < 14:
        text = text.ljust(14, "0")
    return text


def validate_date_range(date_start: str, date_end: str) -> None:
    """Reject invalid or reversed CDX date ranges."""

    if date_start > date_end:
        raise ValueError(
            f"start date {date_start} is after end date {date_end}"
        )


def year_bounds(
    year: int,
    date_start: str,
    date_end: str,
) -> Optional[tuple[str, str]]:
    """Return the intersection of a calendar year with the requested range."""

    year_start = f"{year:04d}0101000000"
    year_end = f"{year:04d}1231235959"
    start = max(year_start, date_start)
    end = min(year_end, date_end)
    if start > end:
        return None
    return start, end


def years_in_range(date_start: str, date_end: str) -> list[int]:
    """Return ascending calendar years intersecting the requested range."""

    return list(range(int(date_start[0:4]), int(date_end[0:4]) + 1))


def _wayback_version() -> str:
    try:
        return importlib.metadata.version("wayback")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _parse_retry_after(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return float(max(0, int(value)))
    except ValueError:
        retry_date = parsedate_tz(value)
        if retry_date is None:
            return None
        return float(max(0, mktime_tz(retry_date) - int(time.time())))


@dataclass(frozen=True)
class YearCdxResult:
    """Raw and parsed annual CDX acquisition result."""

    year: int
    source_dir: Path
    raw_path: Path
    captures: tuple[ParsedCapture, ...]
    failures: tuple[UnresolvedFailure, ...]
    query_meta: dict[str, object]


def fetch_year_cdx(
    layout: CollectionLayout,
    *,
    url_pattern: str,
    year: int,
    date_start: str,
    date_end: str,
    run_id: str,
    session: Optional[requests.Session] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> YearCdxResult:
    """Query one year of CDX, preserve raw bytes, and parse rows."""

    bounds = year_bounds(year, date_start, date_end)
    if bounds is None:
        raise ValueError(f"year {year} is outside {date_start}-{date_end}")
    year_start, year_end = bounds

    ensure_collection_dirs(layout)
    source_dir = layout.sources_root / run_id
    source_dir.mkdir(parents=True, exist_ok=True)

    search_url, match_type = normalize_cdx_search(url_pattern)
    params: dict[str, object] = {
        "url": search_url,
        "from": year_start,
        "to": year_end,
        "output": "json",
        "fl": ",".join(_CDX_FIELDS),
        "showResumeKey": "true",
        "limit": str(CDX_PAGE_LIMIT),
    }
    if match_type is not None:
        params["matchType"] = match_type

    owned_session = session is None
    session = session or ArchiveMagicWaybackSession(user_agent=USER_AGENT)
    raw_pages: list[bytes] = []
    page = 0
    resume_key: Optional[str] = None
    started = time.time()
    request_count = 0

    try:
        while True:
            page_params = dict(params)
            if resume_key is not None:
                page_params["resumeKey"] = resume_key
            query_url = (
                "https://web.archive.org/cdx/search/cdx?"
                + urlencode(page_params)
            )
            body, encoding = _get_cdx_bytes(
                session,
                query_url,
                sleep=sleep,
            )
            request_count += 1
            raw_pages.append(body)
            page += 1
            rows, next_key = _split_cdx_json_pages(body)
            # Persist every page as separate fragments then concatenate.
            page_path = source_dir / f"{year:04d}.page{page:03d}.cdx.json"
            page_path.write_bytes(body)
            if not next_key:
                break
            resume_key = next_key
    finally:
        if owned_session:
            session.close()

    # Materialize one durable annual entity: concatenated page JSON arrays
    # would not be valid JSON, so keep newline-delimited raw page bytes with a
    # separator that is not a CDX field character and record that encoding.
    separator = b"\n#PAGE\n"
    entity = separator.join(raw_pages)
    encoding_label = "nd-json-pages"
    raw_path = source_dir / f"{year:04d}.cdx"
    tmp = exclusive_temp_path(source_dir, suffix=f".{year}.cdx.tmp")
    tmp.write_bytes(entity)
    publish_file_atomically(tmp, raw_path)

    captures: list[ParsedCapture] = []
    failures: list[UnresolvedFailure] = []
    seen: set[CaptureIdentity] = set()
    for page_body in raw_pages:
        page_rows, _ = _split_cdx_json_pages(page_body)
        for raw_line, fields in page_rows:
            parsed = _parse_row(fields, raw_line=raw_line)
            if isinstance(parsed, UnresolvedFailure):
                failures.append(parsed)
                continue
            if parsed.identity in seen:
                continue
            seen.add(parsed.identity)
            captures.append(parsed)

    captures.sort(key=lambda item: item.identity.sort_key())
    query_meta = {
        "year": year,
        "url_pattern": url_pattern,
        "search_url": search_url,
        "match_type": match_type,
        "from": year_start,
        "to": year_end,
        "response_encoding": encoding_label,
        "byte_length": len(entity),
        "sha256": hashlib.sha256(entity).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "client": "archive-magic-fetch+requests",
        "wayback_version": _wayback_version(),
        "request_count": request_count,
        "duration_s": round(time.time() - started, 3),
        "page_count": len(raw_pages),
        "raw_file": raw_path.name,
    }
    _merge_query_json(source_dir, year, query_meta)

    return YearCdxResult(
        year=year,
        source_dir=source_dir,
        raw_path=raw_path,
        captures=tuple(captures),
        failures=tuple(failures),
        query_meta=query_meta,
    )


def parse_raw_cdx_bytes(entity: bytes) -> tuple[list[ParsedCapture], list[UnresolvedFailure]]:
    """Parse one or more raw CDX JSON page bodies separated by #PAGE."""

    captures: list[ParsedCapture] = []
    failures: list[UnresolvedFailure] = []
    seen: set[CaptureIdentity] = set()
    for page_body in entity.split(b"\n#PAGE\n"):
        page_body = page_body.strip()
        if not page_body:
            continue
        rows, _ = _split_cdx_json_pages(page_body)
        for raw_line, fields in rows:
            parsed = _parse_row(fields, raw_line=raw_line)
            if isinstance(parsed, UnresolvedFailure):
                failures.append(parsed)
                continue
            if parsed.identity in seen:
                continue
            seen.add(parsed.identity)
            captures.append(parsed)
    captures.sort(key=lambda item: item.identity.sort_key())
    return captures, failures


def _get_cdx_bytes(
    session: requests.Session,
    url: str,
    *,
    sleep: Callable[[float], None],
    max_attempts: int = 8,
) -> tuple[bytes, str]:
    """GET raw CDX entity bytes with simple retry handling."""

    attempt = 0
    while True:
        attempt += 1
        try:
            response = session.get(url, stream=True, timeout=120)
            if response.status_code == 429:
                delay = _parse_retry_after(
                    response.headers.get("Retry-After")
                ) or 60.0
                response.close()
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"CDX rate limited after {attempt} attempts "
                        f"(retry_after={delay})"
                    )
                sleep(delay)
                continue
            if response.status_code >= 500:
                if attempt >= max_attempts:
                    response.raise_for_status()
                response.close()
                sleep(min(5 * (2**attempt), 300))
                continue
            response.raise_for_status()
            body = response.content
            encoding = response.encoding or "utf-8"
            response.close()
            return body, encoding
        except (requests.ConnectionError, requests.Timeout):
            if attempt >= max_attempts:
                raise
            sleep(min(5 * (2**attempt), 300))


def _split_cdx_json_pages(
    body: bytes,
) -> tuple[list[tuple[str, list[str]]], Optional[str]]:
    """Parse IA CDX JSON that may include a trailing resume key block."""

    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return [], None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # Fallback: treat as CDX line format.
        rows: list[tuple[str, list[str]]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            fields = line.split(" ")
            rows.append((line, fields))
        return rows, None

    if not isinstance(payload, list):
        raise ValueError("CDX JSON response must be a list")

    rows = []
    resume_key: Optional[str] = None
    for index, item in enumerate(payload):
        if item == []:
            # Resume-key separator used by showResumeKey.
            if index + 1 < len(payload) and isinstance(payload[index + 1], list):
                key_row = payload[index + 1]
                if key_row and isinstance(key_row[0], str):
                    resume_key = key_row[0]
            break
        if not isinstance(item, list):
            continue
        # Skip header row when present.
        if item and item[0] == "urlkey":
            continue
        fields = [str(part) for part in item]
        raw_line = " ".join(fields)
        rows.append((raw_line, fields))
    return rows, resume_key


def _parse_row(
    fields: list[str],
    *,
    raw_line: str,
) -> ParsedCapture | UnresolvedFailure:
    """Parse one raw CDX field list into a capture or failure."""

    if len(fields) < 6:
        return _malformed(raw_line, "too few fields")

    urlkey, timestamp, original, mimetype, statuscode, digest = fields[:6]
    # Pad/repair is not applied here: require exact 14-digit timestamps.
    if not re.fullmatch(r"\d{14}", timestamp):
        return _malformed(raw_line, f"invalid timestamp {timestamp!r}")
    if not original:
        return _malformed(raw_line, "missing original URL")

    try:
        identity = make_identity(
            original_url=original,
            timestamp=timestamp,
            status_token=statuscode,
            payload_digest=digest,
            urlkey=urlkey or None,
        )
    except ValueError as error:
        return _malformed(raw_line, str(error))

    # Prefer SURT from original if urlkey field was empty/odd.
    if not urlkey:
        identity = make_identity(
            original_url=original,
            timestamp=timestamp,
            status_token=statuscode,
            payload_digest=digest,
        )

    status_token = cdx_status_token(statuscode)
    digest_token = cdx_payload_digest_token(digest)
    return ParsedCapture(
        identity=identity if identity.payload_digest == digest_token else make_identity(
            original_url=original,
            timestamp=timestamp,
            status_token=status_token,
            payload_digest=digest_token,
            urlkey=identity.urlkey,
        ),
        year=timestamp_year(timestamp),
        is_redirect=is_redirect_status_token(status_token),
        has_usable_digest=normalize_payload_digest(digest) is not None,
        mime=mimetype or "-",
        raw_line=raw_line,
    )


def _malformed(raw_line: str, message: str) -> UnresolvedFailure:
    # Synthetic identity for malformed rows that lack valid fields.
    identity = CaptureIdentity(
        urlkey="-",
        original_url="-",
        timestamp="00000000000000",
        status_token="-",
        payload_digest="-",
    )
    # Try to surface timestamp/url when present for stable sorting.
    parts = raw_line.split(" ")
    if len(parts) >= 3 and re.fullmatch(r"\d{14}", parts[1]):
        identity = CaptureIdentity(
            urlkey=parts[0] or "-",
            original_url=parts[2] or "-",
            timestamp=parts[1],
            status_token=parts[4] if len(parts) > 4 else "-",
            payload_digest=parts[5] if len(parts) > 5 else "-",
        )
    return UnresolvedFailure(
        identity=identity,
        category=FailureCategory.MALFORMED_CDX,
        message=f"{message}: {raw_line[:200]}",
    )


def _merge_query_json(
    source_dir: Path,
    year: int,
    year_meta: dict[str, object],
) -> None:
    path = source_dir / "query.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {
            "schema_version": 1,
            "run_id": source_dir.name,
            "years": {},
        }
    years = data.setdefault("years", {})
    years[str(year)] = year_meta
    tmp = exclusive_temp_path(source_dir, suffix=".query.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    publish_file_atomically(tmp, path)


def init_run_source(layout: CollectionLayout, run_id: str | None = None) -> Path:
    """Create the run source directory and return it."""

    ensure_collection_dirs(layout)
    run_id = run_id or current_run_id()
    source_dir = layout.sources_root / run_id
    source_dir.mkdir(parents=True, exist_ok=True)
    return source_dir
