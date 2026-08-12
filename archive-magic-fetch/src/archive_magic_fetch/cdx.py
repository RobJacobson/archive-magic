"""Annual CDX acquisition, raw persistence, and row parsing."""

from __future__ import annotations

import calendar
import gzip
import hashlib
import importlib.metadata
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
from typing import Callable, Literal, Optional
from urllib.parse import urlencode

import requests
from urllib3.exceptions import ProtocolError
from wayback import WaybackClient, WaybackSession
from wayback._client import read_and_close
from wayback.exceptions import RateLimitError, WaybackRetryError

from .collection import ArchiveLayout, ensure_collection_dirs, exclusive_temp_path, publish_file_atomically
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
from .retry import parse_retry_after


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
_DATE_BOUND = re.compile(r"^\d{4,14}$")


# Gzip member header magic (RFC 1952). Used to detect bodies that are not
# actually compressed despite a Content-Encoding: gzip claim from IA.
_GZIP_MAGIC = b"\x1f\x8b"


class ArchiveMagicWaybackSession(WaybackSession):
    """Wayback session tuned for Archive Magic fetch.

    Library retries stay disabled so Fetch owns its small synchronous retry
    loops and request volume remains explicit.

    Wayback treats any response with ``Memento-Datetime`` as a successful
    memento, which can let HTTP 429 slip through as a playback error with no
    ``retry_after``. Always surface 429 as ``RateLimitError`` and carry an
    explicit `Retry-After` value when IA supplied one.

    Some memento responses also advertise ``Content-Encoding: gzip`` while the
    transfer body is already plaintext (for example HTML starting with
    ``<!DOCTYPE``). ``requests`` then raises ``ContentDecodingError`` when
    reading ``.content``. This session forces ``stream=True``, and for mementos
    that claim gzip it reads the raw body and only decompresses when the gzip
    magic is present.
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("retries", 0)
        super().__init__(*args, **kwargs)

    def send(self, request, **kwargs):
        # requests.Session.send() eagerly reads ``response.content`` unless
        # stream=True. That triggers ContentDecodingError on IA's false gzip
        # claims before we can inspect the raw body, so always defer loading.
        kwargs["stream"] = True
        response = super().send(request, **kwargs)
        if getattr(response, "status_code", None) == 429:
            delay = parse_retry_after(response.headers.get("Retry-After"))
            read_and_close(response)
            raise RateLimitError(response, delay)
        repair_false_gzip_content_encoding(response)
        return response


def repair_false_gzip_content_encoding(response: requests.Response) -> None:
    """Decode memento bodies that falsely claim ``Content-Encoding: gzip``.

    Edge case: Internet Archive occasionally returns a memento with
    ``Content-Encoding: gzip`` whose on-the-wire body is already uncompressed
    (magic bytes are HTML/PDF/etc., not ``\\x1f\\x8b``). urllib3/requests then
    fail with ``ContentDecodingError`` ("incorrect header check").

    Callers must obtain the response with ``stream=True`` (the session
    ``send()`` override does this) so ``requests`` has not already attempted
    content decoding.

    Only memento responses (those with ``Memento-Datetime``) are rewritten, and
    only when they claim gzip. CDX entity downloads keep streaming with
    ``decode_content=False`` and must not have their bodies eagerly consumed
    here. After repair, ``Content-Encoding`` is removed and ``response.content``
    is the logical payload (decompressed when the body was real gzip).

    Mismatched usable bodies are kept for that capture only.
    """

    headers = getattr(response, "headers", None)
    if headers is None or "Memento-Datetime" not in headers:
        return

    encoding = (headers.get("Content-Encoding") or "").split(",")[0].strip().lower()
    if encoding not in {"gzip", "x-gzip"}:
        return

    # Already materialized (for example by a prior hook); do not re-read.
    if getattr(response, "_content", False) is not False:
        return

    raw_stream = getattr(response, "raw", None)
    if raw_stream is None:
        return

    # Disable urllib3's content-decoder so we can inspect the true payload.
    if hasattr(raw_stream, "decode_content"):
        raw_stream.decode_content = False
    raw = raw_stream.read()
    if raw.startswith(_GZIP_MAGIC):
        try:
            body = gzip.decompress(raw)
        except OSError:
            # Truncated or corrupt gzip: keep bytes for caller classification.
            body = raw
    else:
        # False Content-Encoding: IA claimed gzip but sent plaintext. Keep the
        # bytes so the caller can compare them with the CDX digest and retain
        # the response without treating it as reusable when they disagree.
        body = raw

    # Body is now the logical entity; drop the misleading transfer coding.
    try:
        del response.headers["Content-Encoding"]
    except KeyError:
        pass
    response._content = body
    response._content_consumed = True


def make_client() -> WaybackClient:
    """Return the persistent client used for serial playback."""

    return WaybackClient(session=ArchiveMagicWaybackSession(user_agent=USER_AGENT))


def make_cdx_session() -> ArchiveMagicWaybackSession:
    """Return a Wayback session for CDX queries.

    Retries stay at 0 here; ``_get_cdx_entity_bytes`` owns transient backoff so
    connect failures are logged and retried without nesting wayback's loop.
    """

    return ArchiveMagicWaybackSession(user_agent=USER_AGENT)


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
    bound: Literal["start", "end"] = "start",
) -> str:
    """Parse a CDX date bound into a validated 14-digit UTC timestamp.

    Partial values expand to the start or end of that precision in UTC.
    For example, ``2004`` as an end bound becomes ``20041231235959``.
    """

    if value is None or value == "":
        return default
    text = value.strip()
    if not _DATE_BOUND.fullmatch(text):
        raise ValueError(f"invalid date bound: {value!r}")
    if len(text) not in {4, 6, 8, 10, 12, 14}:
        raise ValueError(f"invalid date bound length: {value!r}")

    year = int(text[0:4])
    if len(text) == 4:
        _validate_calendar_date(year, 1, 1)
        if bound == "end":
            return f"{year:04d}1231235959"
        return f"{year:04d}0101000000"

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
        if bound == "end":
            return f"{text}235959"
        return f"{text}000000"

    hour = int(text[8:10])
    if hour > 23:
        raise ValueError(f"invalid date hour: {value!r}")
    if len(text) == 10:
        if bound == "end":
            return f"{text}5959"
        return f"{text}0000"

    minute = int(text[10:12])
    if minute > 59:
        raise ValueError(f"invalid date minute: {value!r}")
    if len(text) == 12:
        if bound == "end":
            return f"{text}59"
        return f"{text}00"

    second = int(text[12:14])
    if second > 59:
        raise ValueError(f"invalid date second: {value!r}")
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
    layout: ArchiveLayout,
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
    collection_id = f"{year:04d}"
    source_dir = layout.run_dir(collection_id, run_id)
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
    session = session or make_cdx_session()
    page_metas: list[dict[str, object]] = []
    page = 0
    resume_key: Optional[str] = None
    started = time.time()
    request_count = 0
    raw_path: Optional[Path] = None

    try:
        while True:
            page_params = dict(params)
            if resume_key is not None:
                page_params["resumeKey"] = resume_key
            query_url = (
                "https://web.archive.org/cdx/search/cdx?"
                + urlencode(page_params)
            )
            entity, content_encoding = _get_cdx_entity_bytes(
                session,
                query_url,
                sleep=sleep,
            )
            request_count += 1
            page += 1
            page_path = _write_raw_cdx_page(
                source_dir,
                year=year,
                page=page,
                entity=entity,
                content_encoding=content_encoding,
            )
            if raw_path is None:
                raw_path = page_path
            stored_entity = page_path.read_bytes()
            page_metas.append(
                {
                    "page": page,
                    "raw_file": page_path.name,
                    "storage_encoding": "gzip",
                    "response_encoding": content_encoding,
                    "byte_length": len(stored_entity),
                    "sha256": hashlib.sha256(stored_entity).hexdigest(),
                    "response_byte_length": len(entity),
                    "response_sha256": hashlib.sha256(entity).hexdigest(),
                    "query_url": query_url,
                }
            )
            # Resume-key discovery only; durable parse happens from disk below.
            parse_body = _decode_cdx_entity(entity, content_encoding)
            _rows, next_key = _split_cdx_json_pages(parse_body)
            if not next_key:
                break
            resume_key = next_key
    finally:
        if owned_session:
            session.close()

    if raw_path is None:
        raise RuntimeError(f"CDX query for {year} returned no response pages")

    # Parse from durable published pages so source and processed input match.
    captures: list[ParsedCapture] = []
    failures: list[UnresolvedFailure] = []
    seen: set[CaptureIdentity] = set()
    for page_meta in page_metas:
        page_path = source_dir / str(page_meta["raw_file"])
        entity = page_path.read_bytes()
        parse_body = _decode_cdx_entity(entity, "gzip")
        page_rows, _ = _split_cdx_json_pages(parse_body)
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
    # Top-level byte_length/sha256/raw_file always describe page one so they
    # stay coherent; pages[] carries per-page totals for multi-page years.
    primary = page_metas[0]
    query_meta = {
        "year": year,
        "url_pattern": url_pattern,
        "search_url": search_url,
        "match_type": match_type,
        "from": year_start,
        "to": year_end,
        "response_encoding": str(primary["response_encoding"]),
        "byte_length": int(primary["byte_length"]),
        "sha256": str(primary["sha256"]),
        "retrieved_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "client": "archive-magic-fetch+requests",
        "wayback_version": _wayback_version(),
        "request_count": request_count,
        "duration_s": round(time.time() - started, 3),
        "page_count": len(page_metas),
        "raw_file": str(primary["raw_file"]),
        "pages": page_metas,
    }
    return YearCdxResult(
        year=year,
        source_dir=source_dir,
        raw_path=raw_path,
        captures=tuple(captures),
        failures=tuple(failures),
        query_meta=query_meta,
    )


def _write_raw_cdx_page(
    source_dir: Path,
    *,
    year: int,
    page: int,
    entity: bytes,
    content_encoding: str,
) -> Path:
    """Persist one CDX HTTP entity before any parsing."""

    page_path = source_dir / f"page-{page:03d}.cdx.gz"
    stored_entity = (
        entity if content_encoding.lower().startswith("gzip") else gzip.compress(entity)
    )
    tmp = exclusive_temp_path(source_dir, suffix=f".{page_path.name}.tmp")
    tmp.write_bytes(stored_entity)
    publish_file_atomically(tmp, page_path)
    return page_path


def _decode_cdx_entity(entity: bytes, content_encoding: str) -> bytes:
    encoding = content_encoding.lower().strip() or "identity"
    if encoding in {"identity", "utf-8", "json"}:
        return entity
    if encoding.startswith("gzip"):
        return gzip.decompress(entity)
    raise ValueError(f"unsupported CDX content encoding: {content_encoding!r}")


def _get_cdx_entity_bytes(
    session: requests.Session,
    url: str,
    *,
    sleep: Callable[[float], None],
    max_attempts: int = 8,
) -> tuple[bytes, str]:
    """GET exact CDX HTTP entity bytes without content-encoding decode.

    Mid-transfer truncations from ``response.raw.read()`` surface as urllib3
    ``ProtocolError`` / ``IncompleteRead`` (not ``requests.ConnectionError``),
    so those are retried here with the same exponential backoff as connect
    failures. Playback treats IncompleteRead as a permanent truncated payload;
    CDX treats it as a failed transfer of an otherwise available index page.
    """

    attempt = 0
    while True:
        attempt += 1
        response = None
        try:
            response = session.get(url, stream=True, timeout=120)
            if response.status_code == 429:
                delay = parse_retry_after(
                    response.headers.get("Retry-After")
                ) or 60.0
                response.close()
                response = None
                if attempt >= max_attempts:
                    raise RuntimeError(
                        f"CDX rate limited after {attempt} attempts "
                        f"(retry_after={delay})"
                    )
                print(
                    f"  rate limit: CDX pausing {delay:g}s "
                    f"(attempt {attempt}/{max_attempts})",
                    flush=True,
                )
                sleep(delay)
                continue
            if response.status_code >= 500:
                status = response.status_code
                if attempt >= max_attempts:
                    response.raise_for_status()
                response.close()
                response = None
                delay = min(5 * (2 ** (attempt - 1)), 300)
                print(
                    f"  CDX server error {status}: "
                    f"retrying in {delay:g}s "
                    f"(attempt {attempt}/{max_attempts})",
                    flush=True,
                )
                sleep(delay)
                continue
            response.raise_for_status()
            headers = getattr(response, "headers", {}) or {}
            content_encoding = str(
                headers.get("Content-Encoding")
                or headers.get("content-encoding")
                or "identity"
            ).lower()
            body = _read_raw_entity_bytes(response)
            response.close()
            response = None
            return body, content_encoding
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
            ProtocolError,
            IncompleteRead,
            WaybackRetryError,
            OSError,
        ) as error:
            if response is not None:
                response.close()
            if attempt >= max_attempts:
                raise
            delay = min(5 * (2 ** (attempt - 1)), 300)
            print(
                f"  CDX connection error: retrying in {delay:g}s "
                f"(attempt {attempt}/{max_attempts}) "
                f"[{type(error).__name__}: {error}]",
                flush=True,
            )
            sleep(delay)


def _read_raw_entity_bytes(response: object) -> bytes:
    """Return the HTTP entity body without decoding Content-Encoding."""

    raw = getattr(response, "raw", None)
    if raw is not None:
        read = getattr(raw, "read", None)
        if callable(read):
            try:
                body = read(decode_content=False)
            except TypeError:
                body = read()
            if isinstance(body, (bytes, bytearray)):
                return bytes(body)
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    raise TypeError("CDX response did not provide readable entity bytes")


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
            # Preserve unexpected JSON entries as deterministic malformed rows
            # rather than silently dropping them from the durable source parse.
            raw_line = json.dumps(item, sort_keys=True, default=str)
            rows.append((raw_line, []))
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
    # Distinct synthetic identity per malformed source row so publication
    # cannot collapse unrelated bad rows in the current run record.
    row_digest = hashlib.sha1(raw_line.encode("utf-8", errors="replace")).hexdigest()
    identity = CaptureIdentity(
        urlkey=f"malformed:{row_digest}",
        original_url="-",
        timestamp="00000000000000",
        status_token="-",
        payload_digest=f"malformed:{row_digest}",
    )
    parts = raw_line.split(" ")
    if len(parts) >= 3 and re.fullmatch(r"\d{14}", parts[1]):
        identity = CaptureIdentity(
            urlkey=parts[0] or f"malformed:{row_digest}",
            original_url=parts[2] or "-",
            timestamp=parts[1],
            status_token=parts[4] if len(parts) > 4 else "-",
            # Always keep the row hash so distinct malformed rows that share
            # timestamp/url fields remain distinct in run.json.
            payload_digest=f"malformed:{row_digest}",
        )
    return UnresolvedFailure(
        identity=identity,
        category=FailureCategory.MALFORMED_CDX,
        message=f"{message}: {raw_line[:200]}",
    )


def init_run_id(layout: ArchiveLayout, run_id: str | None = None) -> str:
    """Allocate one invocation ID shared by every selected collection."""

    ensure_collection_dirs(layout)
    if run_id is not None:
        layout.validate_run_id(run_id)
        if any(layout.captures_root.glob(f"*/runs/{run_id}")):
            raise FileExistsError(f"run ID already exists: {run_id}")
        return run_id

    # Microsecond IDs normally suffice; check all existing collection runs.
    for attempt in range(1000):
        candidate = current_run_id() if attempt == 0 else f"{current_run_id()}-{attempt:02d}"
        if not any(layout.captures_root.glob(f"*/runs/{candidate}")):
            return candidate
    raise RuntimeError("unable to allocate a unique run source directory")
