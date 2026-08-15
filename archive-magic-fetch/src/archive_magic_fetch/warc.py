"""WARC record construction, validation, salvage, and shard writing."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .collection import (
    ArchiveLayout,
    last_collection_warc,
    list_collection_partials,
    parse_warc_partial_name,
    publish_file_atomically,
    warc_artifact_from_path,
)
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
    builder = RecordBuilder(warc_version=RecordBuilder.WARC_1_1)
    return builder.create_warc_record(
        result.identity.original_url,
        "response",
        payload=BytesIO(result.body),
        length=len(result.body),
        http_headers=http_headers,
        warc_headers_dict=warc_headers,
    )


def build_revisit_record(result: RevisitResult):
    """Create a WARC 1.1 revisit record.

    Revisits store current capture identity via CDX extension headers and point
    at an earlier full response via ``WARC-Refers-To-*``. HTTP headers may be
    empty; pywb loads missing HTTP headers from the referenced response.
    """

    http_headers = StatusAndHeaders(
        _status_line(result.http_status_code),
        [],
        protocol="HTTP/1.1",
    )
    builder = RecordBuilder(warc_version=RecordBuilder.WARC_1_1)
    return builder.create_warc_record(
        result.identity.original_url,
        "revisit",
        http_headers=http_headers,
        warc_headers_dict={
            CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
            CDX_STATUS_HEADER: result.identity.status_token,
            CDX_URLKEY_HEADER: result.identity.urlkey,
            "WARC-Date": result.warc_date,
            # Local digest of the referenced payload (may differ from CDX).
            "WARC-Payload-Digest": result.warc_payload_digest,
            "WARC-Profile": (
                "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest"
            ),
            "WARC-Refers-To-Target-URI": result.refers_to_target_uri,
            "WARC-Refers-To-Date": result.refers_to_date,
        },
    )


@dataclass(frozen=True)
class SalvagedWarc:
    """One in-progress WARC promoted to a finalized collection shard."""

    collection_id: str
    sequence: int
    path: Path
    record_count: int


def truncate_incomplete_gzip_warc(path: Path) -> int | None:
    """Keep complete gzip members; return record count or None if unusable."""

    if not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        return None
    good_end = 0
    count = 0
    first_type: str | None = None
    try:
        with path.open("rb") as stream:
            iterator = ArchiveIterator(stream, check_digests="raise")
            for record in iterator:
                if first_type is None:
                    first_type = record.rec_type
                record.raw_stream.read()
                count += 1
                good_end = (
                    iterator.get_record_offset() + iterator.get_record_length()
                )
    except Exception:  # noqa: BLE001 - torn gzip member is expected
        pass
    if first_type != "warcinfo" or count < 2 or good_end <= 0:
        path.unlink(missing_ok=True)
        return None
    size = path.stat().st_size
    if good_end < size:
        os.truncate(path, good_end)
    return count


def salvage_collection_partials(layout: ArchiveLayout) -> list[SalvagedWarc]:
    """Promote visible in-progress WARC partials into finalized shards."""

    salvaged: list[SalvagedWarc] = []
    if not layout.collections_root.is_dir():
        return salvaged
    for collection_dir in sorted(layout.collections_root.iterdir()):
        if not collection_dir.is_dir():
            continue
        try:
            collection_id = layout.validate_collection_id(collection_dir.name)
        except ValueError:
            continue
        for path in list_collection_partials(layout, collection_id):
            sequence = parse_warc_partial_name(layout, collection_id, path.name)
            if sequence is None:
                continue
            artifact = _publish_salvaged_partial(
                layout, collection_id, sequence, path
            )
            if artifact is not None:
                salvaged.append(artifact)
    return salvaged


def _publish_salvaged_partial(
    layout: ArchiveLayout,
    collection_id: str,
    sequence: int,
    path: Path,
) -> SalvagedWarc | None:
    """Truncate a partial and replace the shard only when it is an improvement."""

    record_count = truncate_incomplete_gzip_warc(path)
    if record_count is None:
        return None
    final_path = layout.collection_warc_path(collection_id, sequence)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.is_file() and path.stat().st_size <= final_path.stat().st_size:
        path.unlink(missing_ok=True)
        return None
    publish_file_atomically(path, final_path)
    return SalvagedWarc(
        collection_id=collection_id,
        sequence=sequence,
        path=final_path,
        record_count=record_count,
    )


@dataclass
class CollectionWarcWriter:
    """Single-owner WARC writer for one portable collection."""

    layout: ArchiveLayout
    collection_id: str
    target_bytes: int = DEFAULT_WARC_TARGET_BYTES
    sequence: int = 0
    stream: BinaryIO | None = None
    writer: WARCWriter | None = None
    temp_path: Path | None = None
    record_count: int = 0
    finalized: list[WarcArtifact] = field(default_factory=list)
    _continue_from: Path | None = None

    def __post_init__(self) -> None:
        if self.sequence != 0:
            return
        last = last_collection_warc(self.layout, self.collection_id)
        if last is None:
            self.sequence = 1
            return
        sequence, path = last
        if path.stat().st_size < self.target_bytes:
            self.sequence = sequence
            self._continue_from = path
            return
        nxt = sequence + 1
        if nxt > 999:
            raise RuntimeError(
                f"WARC sequence would exceed 999 for collection {self.collection_id}"
            )
        self.sequence = nxt

    def write_playback(self, result: PlaybackResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_response_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._flush()
        self._maybe_rotate()

    def write_revisit(self, result: RevisitResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_revisit_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._flush()
        self._maybe_rotate()

    def close(self) -> list[WarcArtifact]:
        """Finalize any open shard and return all newly published WARCs."""

        if self.stream is not None or self.temp_path is not None:
            self._finalize_current()
        return list(self.finalized)

    def _flush(self) -> None:
        if self.stream is not None:
            self.stream.flush()

    def _ensure_open(self) -> None:
        if self.writer is not None:
            return
        if self.sequence > 999:
            raise RuntimeError(
                f"WARC sequence would exceed 999 for collection {self.collection_id}"
            )
        collection_dir = self.layout.collection_dir(self.collection_id)
        collection_dir.mkdir(parents=True, exist_ok=True)
        partial = self.layout.collection_warc_partial_path(
            self.collection_id, self.sequence
        )
        final_name = self.layout.collection_warc_filename(
            self.collection_id, self.sequence
        )
        continue_from = self._continue_from
        self._continue_from = None
        if continue_from is not None and continue_from.is_file():
            shutil.copyfile(continue_from, partial)
            self.stream = partial.open("ab")
            self.temp_path = partial
            self.writer = WARCWriter(
                self.stream,
                gzip=True,
                warc_version=RecordBuilder.WARC_1_1,
            )
            self.record_count = 0
            return
        self.temp_path = partial
        self.stream = partial.open("xb")
        self.writer = WARCWriter(
            self.stream,
            gzip=True,
            warc_version=RecordBuilder.WARC_1_1,
        )
        warcinfo = self.writer.create_warcinfo_record(
            final_name,
            {},
        )
        self.writer.write_record(warcinfo)
        self._flush()
        self.record_count = 1  # warcinfo counts as a record for size only

    def _maybe_rotate(self) -> None:
        assert self.temp_path is not None
        if self.temp_path.stat().st_size < self.target_bytes:
            return
        self._finalize_current()

    def _finalize_current(self) -> None:
        assert self.temp_path is not None
        if self.stream is not None:
            try:
                self.stream.flush()
            except OSError:
                pass
            try:
                self.stream.close()
            except OSError:
                pass
        self.stream = None
        self.writer = None
        record_count = truncate_incomplete_gzip_warc(self.temp_path)
        if record_count is None:
            self.temp_path = None
            self.record_count = 0
            return
        final_path = self.layout.collection_warc_path(
            self.collection_id, self.sequence
        )
        publish_file_atomically(self.temp_path, final_path)
        artifact = warc_artifact_from_path(
            self.layout,
            final_path,
            record_count=record_count,
        )
        self.finalized.append(artifact)
        self.temp_path = None
        self.record_count = 0
        self.sequence += 1
        self._continue_from = None


def _scan_warc(path: Path, *, check_digests: bool | str) -> tuple[int, str | None]:
    """Consume a WARC once and return its count and first record type."""

    count = 0
    first_type: str | None = None
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests=check_digests):
            if first_type is None:
                first_type = record.rec_type
            count += 1
            record.raw_stream.read()
    return count, first_type


def validate_warc(path: Path) -> int:
    """Require a parseable WARC with valid digests and return its record count."""

    count, first_type = _scan_warc(path, check_digests="raise")
    if first_type != "warcinfo":
        raise ValueError(f"WARC missing leading warcinfo: {path}")
    return count
