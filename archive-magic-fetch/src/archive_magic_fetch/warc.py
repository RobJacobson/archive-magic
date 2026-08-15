"""WARC record construction and append-only shard writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from io import BytesIO
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .collection import ArchiveLayout, last_collection_warc, warc_artifact_from_path
from .config import DEFAULT_WARC_TARGET_BYTES
from .models import PlaybackResult, RevisitResult, WarcArtifact
from .protocol import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
)


def _status_line(status_code: int) -> str:
    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def build_response_record(result: PlaybackResult):
    """Create a WARC 1.1 response record for a playback result."""

    http_headers = StatusAndHeaders(
        _status_line(result.status_code),
        list(result.headers),
        protocol="HTTP/1.1",
    )
    warc_headers = {
        CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
        CDX_STATUS_HEADER: result.identity.status_token,
        CDX_URLKEY_HEADER: result.identity.urlkey,
        "WARC-Date": result.warc_date,
        "WARC-Source-URI": result.source_uri,
        "WARC-Payload-Digest": result.warc_payload_digest,
    }
    if not result.digest_matched:
        warc_headers[CDX_DIGEST_MATCH_HEADER] = "false"
    return RecordBuilder(warc_version=RecordBuilder.WARC_1_1).create_warc_record(
        result.identity.original_url,
        "response",
        payload=BytesIO(result.body),
        length=len(result.body),
        http_headers=http_headers,
        warc_headers_dict=warc_headers,
    )


def build_revisit_record(result: RevisitResult):
    """Create a WARC 1.1 revisit record."""

    return RecordBuilder(warc_version=RecordBuilder.WARC_1_1).create_warc_record(
        result.identity.original_url,
        "revisit",
        http_headers=StatusAndHeaders(
            _status_line(result.http_status_code),
            [],
            protocol="HTTP/1.1",
        ),
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


def _serialize_record(record) -> bytes:
    """Serialize and validate one independent gzip member before appending it."""

    stream = BytesIO()
    WARCWriter(
        stream,
        gzip=True,
        warc_version=RecordBuilder.WARC_1_1,
    ).write_record(record)
    data = stream.getvalue()
    with BytesIO(data) as source:
        records = ArchiveIterator(source, check_digests="raise")
        parsed = next(records)
        parsed.raw_stream.read()
        try:
            next(records)
        except StopIteration:
            pass
        else:
            raise ValueError("serialized WARC member contained multiple records")
    return data


def _warcinfo(filename: str) -> bytes:
    stream = BytesIO()
    writer = WARCWriter(
        stream,
        gzip=True,
        warc_version=RecordBuilder.WARC_1_1,
    )
    return _serialize_record(writer.create_warcinfo_record(filename, {}))


def _append_bytes(path: Path, data: bytes) -> None:
    """Append one validated member and restore the previous length on failure."""

    previous_size = path.stat().st_size if path.is_file() else 0
    try:
        with path.open("ab") as stream:
            written = stream.write(data)
            if written != len(data):
                raise OSError(f"short WARC append: wrote {written} of {len(data)} bytes")
            stream.flush()
    except BaseException:
        if path.is_file():
            with path.open("r+b") as stream:
                stream.truncate(previous_size)
        raise


@dataclass
class CollectionWarcWriter:
    """Append validated records to the current shard, rotating at the size target."""

    layout: ArchiveLayout
    collection_id: str
    target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    sequence: int = 0
    finalized: list[WarcArtifact] = field(default_factory=list)
    current_path: Path | None = None
    _base_size: int = 0
    _changed: bool = False

    def __post_init__(self) -> None:
        if self.sequence:
            return
        last = last_collection_warc(self.layout, self.collection_id)
        if last is None:
            self.sequence = 1
        elif last[1].stat().st_size < self.target_bytes:
            self.sequence, self.current_path = last
            self._base_size = self.current_path.stat().st_size
        else:
            self.sequence = last[0] + 1

    def write_playback(self, result: PlaybackResult) -> None:
        self._append_record(_serialize_record(build_response_record(result)))

    def write_revisit(self, result: RevisitResult) -> None:
        self._append_record(_serialize_record(build_revisit_record(result)))

    def close(self) -> list[WarcArtifact]:
        """Validate changed shards and return their current artifact metadata."""

        self._finalize_current()
        return list(self.finalized)

    def _append_record(self, data: bytes) -> None:
        self._ensure_current()
        assert self.current_path is not None
        _append_bytes(self.current_path, data)
        self._changed = True
        if self.current_path.stat().st_size >= self.target_bytes:
            self._finalize_current()

    def _ensure_current(self) -> None:
        if self.current_path is not None:
            return
        path = self.layout.collection_warc_path(self.collection_id, self.sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.current_path = path
        self._base_size = path.stat().st_size if path.is_file() else 0
        if self._base_size == 0:
            _append_bytes(path, _warcinfo(path.name))
            self._changed = True

    def _finalize_current(self) -> None:
        path = self.current_path
        if path is None or not self._changed:
            return
        try:
            count = validate_warc(path)
        except BaseException:
            if self._base_size:
                with path.open("r+b") as stream:
                    stream.truncate(self._base_size)
            else:
                path.unlink(missing_ok=True)
            self._reset_current()
            raise
        self.finalized.append(
            warc_artifact_from_path(self.layout, path, record_count=count)
        )
        self._reset_current()
        self.sequence += 1

    def _reset_current(self) -> None:
        self.current_path = None
        self._base_size = 0
        self._changed = False


def validate_warc(path: Path) -> int:
    """Require a complete WARC with valid digests and at least one capture."""

    count = 0
    first_type: str | None = None
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests="raise"):
            if first_type is None:
                first_type = record.rec_type
            record.raw_stream.read()
            count += 1
    if first_type != "warcinfo":
        raise ValueError(f"WARC missing leading warcinfo: {path}")
    if count < 2:
        raise ValueError(f"WARC contains no captures: {path}")
    return count
