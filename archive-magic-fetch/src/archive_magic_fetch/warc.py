"""WARC 1.1 inventory, exact playback writing, and size-bounded rollover."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Mapping, Optional

from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter
from wayback import Mode
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
)

from .collection import (
    CollectionLayout,
    exclusive_temp_path,
    list_all_warcs,
    next_warc_sequence,
    publish_file_atomically,
    warc_artifact_from_path,
)
from .models import (
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
    MISSING_CDX_PAYLOAD_DIGEST,
    MISSING_CDX_STATUS,
    SOFTWARE_ID,
    WARC_TARGET_BYTES,
    WARC_VERSION,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    RevisitResult,
    WarcArtifact,
    cdx_payload_digest_token,
    cdx_status_token,
    cdx_timestamp_to_warc_date,
    make_identity,
    normalize_payload_digest,
    timestamp_to_warc_date,
    warc_date_to_cdx,
)


_REPRESENTATION_HEADERS = {
    "content-digest",
    "content-encoding",
    "content-length",
    "content-md5",
    "digest",
    "etag",
    "repr-digest",
    "transfer-encoding",
}


@dataclass(frozen=True)
class StoredResponse:
    """Location of one full response within a year."""

    identity: CaptureIdentity
    relative_key: str
    warc_date: str
    warc_payload_digest: str
    target_uri: str
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes | None = None


@dataclass
class YearResponseIndex:
    """In-year full responses available for revisit targets."""

    by_identity: dict[CaptureIdentity, StoredResponse] = field(
        default_factory=dict
    )
    by_url_digest: dict[tuple[str, str], StoredResponse] = field(
        default_factory=dict
    )


@dataclass
class CollectionInventory:
    """Exact captures already present in the collection."""

    identities: set[CaptureIdentity] = field(default_factory=set)
    # year -> urlkey/digest -> stored response metadata for revisits
    year_responses: dict[int, YearResponseIndex] = field(default_factory=dict)

    def contains(self, identity: CaptureIdentity) -> bool:
        return identity in self.identities

    def year_index(self, year: int) -> YearResponseIndex:
        return self.year_responses.setdefault(year, YearResponseIndex())


def payload_digest(payload: bytes) -> str:
    """Return a CDX-compatible SHA-1 digest of payload bytes."""

    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def get_warc_identity(record) -> CaptureIdentity:
    """Rebuild capture identity from WARC extension headers."""

    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    warc_date = record.rec_headers.get_header("WARC-Date")
    cdx_digest = record.rec_headers.get_header(CDX_PAYLOAD_DIGEST_HEADER)
    cdx_status = record.rec_headers.get_header(CDX_STATUS_HEADER)
    cdx_urlkey = record.rec_headers.get_header(CDX_URLKEY_HEADER)
    if not target_uri or not warc_date:
        raise ValueError("WARC record is missing target URI or date")
    if cdx_digest is None:
        raise ValueError(f"WARC record is missing {CDX_PAYLOAD_DIGEST_HEADER}")
    if cdx_status is None:
        raise ValueError(f"WARC record is missing {CDX_STATUS_HEADER}")
    digest_token = cdx_payload_digest_token(cdx_digest)
    if (
        normalize_payload_digest(cdx_digest) is None
        and cdx_digest.strip() != MISSING_CDX_PAYLOAD_DIGEST
    ):
        raise ValueError(
            f"WARC record has invalid {CDX_PAYLOAD_DIGEST_HEADER}"
        )
    return make_identity(
        original_url=target_uri,
        timestamp=warc_date_to_cdx(warc_date),
        status_token=cdx_status_token(cdx_status),
        payload_digest=digest_token,
        urlkey=cdx_urlkey or None,
    )


def inventory_collection(layout: CollectionLayout) -> CollectionInventory:
    """Validate and inventory every existing collection WARC."""

    inv = CollectionInventory()
    for path in list_all_warcs(layout):
        relative = path.relative_to(layout.root).as_posix()
        year = _year_from_warc_path(path)
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream, check_digests="raise"):
                if record.rec_type not in {"response", "revisit"}:
                    record.raw_stream.read()
                    continue
                identity = get_warc_identity(record)
                inv.identities.add(identity)
                warc_payload = normalize_payload_digest(
                    record.rec_headers.get_header("WARC-Payload-Digest")
                )
                if record.rec_type == "response" and warc_payload is not None:
                    status = int(record.http_headers.get_statuscode())
                    headers = tuple(record.http_headers.headers)
                    # Do not retain full bodies in inventory for memory.
                    stored = StoredResponse(
                        identity=identity,
                        relative_key=relative,
                        warc_date=record.rec_headers.get_header("WARC-Date"),
                        warc_payload_digest=warc_payload,
                        target_uri=identity.original_url,
                        status_code=status,
                        headers=headers,
                        body=None,
                    )
                    yindex = inv.year_index(year)
                    yindex.by_identity[identity] = stored
                    if identity.payload_digest != MISSING_CDX_PAYLOAD_DIGEST:
                        yindex.by_url_digest[
                            (identity.urlkey, identity.payload_digest)
                        ] = stored
                # consume body stream
                record.raw_stream.read()
    return inv


def download_exact(
    client,
    capture_url: str,
    timestamp: str,
    *,
    expected_status: Optional[int],
    expected_url: str,
) -> PlaybackResult:
    """Fetch one exact memento and return a validated playback result."""

    memento = client.get_memento(
        capture_url,
        timestamp=timestamp,
        mode=Mode.original,
        exact=True,
        follow_redirects=False,
    )
    with memento:
        body = memento.content
        status_code = memento.status_code
        memento_url = memento.memento_url
        memento_timestamp = memento.timestamp
        headers = tuple(
            _semantic_headers(memento.headers, len(body), status_code=status_code)
        )
        url = memento.url

    returned_ts = timestamp_to_warc_date(memento_timestamp)
    returned_cdx = warc_date_to_cdx(returned_ts)
    if returned_cdx != timestamp:
        raise ExactMismatchError(
            f"timestamp mismatch: requested {timestamp}, got {returned_cdx}"
        )
    if url != expected_url:
        raise ExactMismatchError(
            f"URL mismatch: requested {expected_url}, got {url}"
        )
    if expected_status is not None and status_code != expected_status:
        raise ExactMismatchError(
            f"status mismatch: requested {expected_status}, got {status_code}"
        )

    return PlaybackResult(
        identity=make_identity(
            original_url=expected_url,
            timestamp=timestamp,
            status_token=(
                str(expected_status)
                if expected_status is not None
                else MISSING_CDX_STATUS
            ),
            payload_digest=MISSING_CDX_PAYLOAD_DIGEST,
        ),
        body=body,
        status_code=status_code,
        headers=headers,
        warc_date=returned_ts,
        source_uri=memento_url,
        warc_payload_digest=payload_digest(body),
    )


def download_exact_for_identity(client, identity: CaptureIdentity) -> PlaybackResult:
    """Exact-playback one capture identity and attach full identity fields."""

    expected_status = (
        int(identity.status_token) if identity.status_token.isdigit() else None
    )
    result = download_exact(
        client,
        identity.original_url,
        identity.timestamp,
        expected_status=expected_status,
        expected_url=identity.original_url,
    )
    actual_digest = result.warc_payload_digest
    expected_digest = normalize_payload_digest(identity.payload_digest)
    if expected_digest is not None and actual_digest != expected_digest:
        raise DigestValidationError(
            f"payload digest mismatch: CDX {expected_digest}, "
            f"downloaded {actual_digest}"
        )
    return PlaybackResult(
        identity=identity,
        body=result.body,
        status_code=result.status_code,
        headers=result.headers,
        warc_date=result.warc_date,
        source_uri=result.source_uri,
        warc_payload_digest=actual_digest,
    )


class ExactMismatchError(MementoPlaybackError):
    """Returned memento is not the requested capture."""


class DigestValidationError(Exception):
    """Downloaded payload does not match the CDX payload digest."""


_RETRYABLE_HTTP_STATUSES = frozenset({413, 421, 429, 500, 502, 503, 504, 599})
_STATUS_IN_MESSAGE = re.compile(r"\b([45]\d\d)\b")


def classify_playback_error(error: BaseException) -> tuple[FailureCategory, bool]:
    """Return (category, retryable) for a playback error."""

    if isinstance(error, ExactMismatchError):
        return FailureCategory.EXACT_MISMATCH, False
    if isinstance(error, DigestValidationError):
        return FailureCategory.DIGEST_VALIDATION, False
    if isinstance(error, (BlockedByRobotsError, BlockedSiteError)):
        return FailureCategory.BLOCKED, False
    name = type(error).__name__
    if "RateLimit" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    if "Retryable" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    status = getattr(error, "status_code", None)
    if status is None:
        match = _STATUS_IN_MESSAGE.search(str(error))
        if match:
            status = int(match.group(1))
    if status in _RETRYABLE_HTTP_STATUSES:
        return FailureCategory.RETRY_EXHAUSTED, True
    if isinstance(error, MementoPlaybackError):
        return FailureCategory.UNAVAILABLE, False
    if "Timeout" in name or "Connection" in name or "Chunked" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    if "IncompleteRead" in name or "Truncat" in name:
        return FailureCategory.TRUNCATED, True
    return FailureCategory.UNAVAILABLE, False


def _semantic_headers(
    headers: Mapping[str, str],
    payload_length: int,
    *,
    status_code: int,
) -> list[tuple[str, str]]:
    skip = set(_REPRESENTATION_HEADERS)
    # Preserve Content-Range for partial responses; the stored body is that
    # range and replay needs the header to describe it.
    if status_code != 206:
        skip.add("content-range")
    semantic = [
        (name, value)
        for name, value in headers.items()
        if name.lower() not in skip
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic


def _status_line(status_code: int) -> str:
    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def _year_from_warc_path(path: Path) -> int:
    # archive/YYYY/…
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "archive" and index + 1 < len(parts):
            return int(parts[index + 1])
    raise ValueError(f"cannot determine year for {path}")


def build_response_record(result: PlaybackResult):
    """Create a WARC 1.1 response record for a playback result."""

    http_headers = StatusAndHeaders(
        _status_line(result.status_code),
        list(result.headers),
        protocol="HTTP/1.1",
    )
    builder = RecordBuilder(warc_version=WARC_VERSION)
    return builder.create_warc_record(
        result.identity.original_url,
        "response",
        payload=BytesIO(result.body),
        length=len(result.body),
        http_headers=http_headers,
        warc_headers_dict={
            CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
            CDX_STATUS_HEADER: result.identity.status_token,
            CDX_URLKEY_HEADER: result.identity.urlkey,
            "WARC-Date": result.warc_date,
            "WARC-Source-URI": result.source_uri,
            "WARC-Payload-Digest": result.warc_payload_digest,
        },
    )


def build_revisit_record(result: RevisitResult):
    """Create a WARC 1.1 revisit record."""

    http_headers = StatusAndHeaders(
        _status_line(result.http_status_code),
        list(result.http_headers),
        protocol="HTTP/1.1",
    )
    builder = RecordBuilder(warc_version=WARC_VERSION)
    return builder.create_warc_record(
        result.identity.original_url,
        "revisit",
        http_headers=http_headers,
        warc_headers_dict={
            CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
            CDX_STATUS_HEADER: result.identity.status_token,
            CDX_URLKEY_HEADER: result.identity.urlkey,
            "WARC-Date": result.warc_date,
            "WARC-Payload-Digest": result.warc_payload_digest,
            "WARC-Profile": (
                "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest"
            ),
            "WARC-Refers-To-Target-URI": result.refers_to_target_uri,
            "WARC-Refers-To-Date": result.refers_to_date,
        },
    )


@dataclass
class YearWarcWriter:
    """Single-owner WARC writer for one calendar year."""

    layout: CollectionLayout
    year: int
    target_bytes: int = WARC_TARGET_BYTES
    sequence: int = 0
    stream: BinaryIO | None = None
    writer: WARCWriter | None = None
    temp_path: Path | None = None
    record_count: int = 0
    finalized: list[WarcArtifact] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.sequence == 0:
            self.sequence = next_warc_sequence(self.layout, self.year)

    def write_playback(self, result: PlaybackResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_response_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._maybe_rotate()

    def write_revisit(self, result: RevisitResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_revisit_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._maybe_rotate()

    def close(self) -> list[WarcArtifact]:
        """Finalize any open shard and return all newly published WARCs."""

        if self.stream is not None:
            self._finalize_current()
        return list(self.finalized)

    def _ensure_open(self) -> None:
        if self.writer is not None:
            return
        if self.sequence > 999:
            raise RuntimeError(
                f"WARC sequence would exceed 999 for year {self.year}"
            )
        year_dir = self.layout.year_dir(self.year)
        year_dir.mkdir(parents=True, exist_ok=True)
        final_name = self.layout.warc_filename(self.year, self.sequence)
        self.temp_path = exclusive_temp_path(
            year_dir,
            suffix=f".{final_name}.partial",
        )
        self.stream = self.temp_path.open("xb")
        self.writer = WARCWriter(
            self.stream,
            gzip=True,
            warc_version=WARC_VERSION,
        )
        warcinfo = self.writer.create_warcinfo_record(
            final_name,
            {
                "software": SOFTWARE_ID,
                "format": f"WARC File Format {WARC_VERSION}",
            },
        )
        self.writer.write_record(warcinfo)
        self.record_count = 1  # warcinfo counts as a record for size only

    def _maybe_rotate(self) -> None:
        assert self.temp_path is not None
        if self.temp_path.stat().st_size < self.target_bytes:
            return
        self._finalize_current()

    def _finalize_current(self) -> None:
        assert self.stream is not None
        assert self.temp_path is not None
        self.stream.close()
        self.stream = None
        self.writer = None
        validate_warc(self.temp_path)
        final_path = self.layout.warc_path(self.year, self.sequence)
        publish_file_atomically(self.temp_path, final_path)
        # record_count includes warcinfo; store total records written
        artifact = warc_artifact_from_path(
            self.layout,
            final_path,
            record_count=self.record_count,
        )
        self.finalized.append(artifact)
        self.temp_path = None
        self.record_count = 0
        self.sequence += 1


def validate_warc(path: Path) -> None:
    """Require one fully parseable WARC with valid digests."""

    types: list[str] = []
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests="raise"):
            types.append(record.rec_type)
            record.raw_stream.read()
    if not types or types[0] != "warcinfo":
        raise ValueError(f"WARC missing leading warcinfo: {path}")


def count_warc_records(path: Path) -> int:
    """Return the number of records in a finalized WARC."""

    count = 0
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests=False):
            count += 1
            record.raw_stream.read()
    return count


def revisit_from_stored(
    identity: CaptureIdentity,
    stored: StoredResponse,
    *,
    http_status_code: int | None = None,
    http_headers: tuple[tuple[str, str], ...] | None = None,
) -> RevisitResult:
    """Build a revisit referencing an in-year full response."""

    return RevisitResult(
        identity=identity,
        warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
        refers_to_target_uri=stored.target_uri,
        refers_to_date=stored.warc_date,
        warc_payload_digest=stored.warc_payload_digest,
        http_status_code=http_status_code or stored.status_code,
        http_headers=http_headers or stored.headers,
    )


def stored_from_playback(
    layout: CollectionLayout,
    year: int,
    result: PlaybackResult,
    relative_key: str,
) -> StoredResponse:
    """Create inventory metadata for a just-written full response."""

    return StoredResponse(
        identity=result.identity,
        relative_key=relative_key,
        warc_date=result.warc_date,
        warc_payload_digest=result.warc_payload_digest,
        target_uri=result.identity.original_url,
        status_code=result.status_code,
        headers=result.headers,
        body=None,
    )
