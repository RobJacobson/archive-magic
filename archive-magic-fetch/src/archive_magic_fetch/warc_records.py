"""WARC 1.0 record preparation and writing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Optional

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from .capture_identity import (
    CaptureIdentity,
    identity_for_capture,
    identity_for_warc_record,
)


@dataclass(frozen=True)
class CachedWarcResponse:
    """One validated semantic response loaded from an existing WARC."""

    body: bytes
    status_code: int
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _StoredRecordLocation:
    identity: CaptureIdentity
    offset: int
    length: int
    record_type: str


class ExistingWarcCache:
    """Validated semantic inventory backed by one existing WARC."""

    def __init__(
        self,
        path: Path,
        entries: dict[CaptureIdentity, _StoredRecordLocation],
        records: tuple[_StoredRecordLocation, ...],
        responses_by_digest: dict[str, _StoredRecordLocation],
        references_by_urlkey: dict[str, dict[str, RevisitReference]],
    ) -> None:
        self.path = path
        self._entries = entries
        self._records = records
        self._responses_by_digest = responses_by_digest
        self._references_by_urlkey = references_by_urlkey

    @classmethod
    def inventory(cls, path: Path) -> ExistingWarcCache:
        """Validate a WARC and inventory every logical response/revisit."""

        entries: dict[CaptureIdentity, _StoredRecordLocation] = {}
        records: list[_StoredRecordLocation] = []
        responses_by_digest: dict[str, _StoredRecordLocation] = {}
        references_by_urlkey: dict[str, dict[str, RevisitReference]] = {}
        record_types: list[str] = []
        stream = path.open("rb")
        try:
            iterator = ArchiveIterator(stream, check_digests="raise")
            for record in iterator:
                record_types.append(record.rec_type)
                if record.rec_type not in {"response", "revisit"}:
                    record.raw_stream.read()
                    continue
                identity = identity_for_warc_record(record)
                offset = iterator.get_record_offset()
                length = iterator.get_record_length()
                location = _StoredRecordLocation(
                    identity=identity,
                    offset=offset,
                    length=length,
                    record_type=record.rec_type,
                )
                if identity not in entries:
                    entries[identity] = location
                    records.append(location)
                if record.rec_type == "response":
                    reference = response_reference(record)
                    responses_by_digest.setdefault(
                        identity.payload_digest,
                        location,
                    )
                    references_by_urlkey.setdefault(
                        identity.urlkey,
                        {},
                    ).setdefault(identity.payload_digest, reference)
                record.raw_stream.read()
            if iterator.err_count:
                raise ValueError(
                    f"existing WARC contains {iterator.err_count} "
                    "malformed record boundary warning(s)"
                )
            if not record_types or record_types[0] != "warcinfo":
                raise ValueError("existing WARC is missing its initial warcinfo")
            if not records:
                raise ValueError("existing WARC contains no response or revisit")
            return cls(
                path,
                entries,
                tuple(records),
                responses_by_digest,
                references_by_urlkey,
            )
        except Exception:
            stream.close()
            raise
        finally:
            if not stream.closed:
                stream.close()

    @property
    def identities(self) -> frozenset[CaptureIdentity]:
        """Return every logical capture in this WARC."""

        return frozenset(self._entries)

    @property
    def response_count(self) -> int:
        return sum(record.record_type == "response" for record in self._records)

    @property
    def revisit_count(self) -> int:
        return sum(record.record_type == "revisit" for record in self._records)

    def reference_groups(self):
        """Iterate normalized URL keys and their response representatives."""

        return self._references_by_urlkey.items()

    def contains(self, capture) -> bool:
        """Return whether this WARC already contains a logical capture."""

        return identity_for_capture(capture) in self._entries

    def _member(self, location: _StoredRecordLocation) -> bytes:
        with self.path.open("rb") as stream:
            stream.seek(location.offset)
            member = stream.read(location.length)
        if len(member) != location.length:
            raise ValueError("compressed WARC member is truncated")
        return member

    def _record(self, location: _StoredRecordLocation):
        iterator = ArchiveIterator(
            BytesIO(self._member(location)),
            check_digests="raise",
        )
        record = next(iterator)
        if identity_for_warc_record(record) != location.identity:
            raise ValueError("cached record identity changed")
        return iterator, record

    def copy_records(self, writer: WARCWriter) -> None:
        """Copy the validated semantic baseline into a replacement WARC."""

        with self.path.open("rb") as stream:
            for location in self._records:
                stream.seek(location.offset)
                member = stream.read(location.length)
                if len(member) != location.length:
                    raise ValueError("compressed WARC member is truncated")
                iterator = ArchiveIterator(
                    BytesIO(member),
                    check_digests="raise",
                )
                record = next(iterator)
                if identity_for_warc_record(record) != location.identity:
                    raise ValueError("cached record identity changed")
                writer.write_record(record)
                try:
                    next(iterator)
                except StopIteration:
                    pass
                else:
                    raise ValueError("cached member contains multiple records")
                if iterator.err_count:
                    raise ValueError("cached member has a malformed boundary")

    def get(self, capture) -> Optional[CachedWarcResponse]:
        """Load the payload represented by an exact logical capture."""

        identity = identity_for_capture(capture)
        location = self._entries.get(identity)
        if location is None:
            return None

        try:
            exact_iterator, exact_record = self._record(location)
            status_code = int(exact_record.http_headers.get_statuscode())
            headers = tuple(exact_record.http_headers.headers)
            if location.record_type == "response":
                body = exact_record.content_stream().read()
            else:
                body_location = self._responses_by_digest.get(
                    identity.payload_digest
                )
                if body_location is None:
                    raise ValueError(
                        "revisit has no local full-response representative"
                    )
                _body_iterator, body_record = self._record(body_location)
                body = body_record.content_stream().read()
                if not headers:
                    headers = tuple(body_record.http_headers.headers)
            return CachedWarcResponse(
                body=body,
                status_code=status_code,
                headers=headers,
            )
        except Exception as error:
            raise ValueError(
                f"cannot reuse response from {self.path}: {error}"
            ) from error

class ExistingWarcCollection:
    """One validated, normalized cache spanning every collection WARC."""

    def __init__(self, caches: dict[Path, ExistingWarcCache]) -> None:
        self._caches = caches
        self._identity_caches: dict[CaptureIdentity, ExistingWarcCache] = {}
        self._references_by_urlkey: dict[str, dict[str, RevisitReference]] = {}
        for cache in caches.values():
            for identity in cache.identities:
                self._identity_caches.setdefault(identity, cache)
            for urlkey, references in cache.reference_groups():
                target = self._references_by_urlkey.setdefault(urlkey, {})
                for digest, reference in references.items():
                    target.setdefault(digest, reference)

    @classmethod
    def inventory(cls, paths) -> ExistingWarcCollection:
        """Inventory and validate all existing collection WARC files."""

        caches: dict[Path, ExistingWarcCache] = {}
        for path in paths:
            try:
                caches[path] = ExistingWarcCache.inventory(path)
            except Exception as error:
                raise ValueError(
                    f"cannot inventory existing WARC {path}: {error}"
                ) from error
        return cls(caches)

    def cache_for(self, path: Path) -> Optional[ExistingWarcCache]:
        return self._caches.get(path)

    def contains(self, capture) -> bool:
        return identity_for_capture(capture) in self._identity_caches

    def get(self, capture) -> Optional[CachedWarcResponse]:
        cache = self._identity_caches.get(identity_for_capture(capture))
        return cache.get(capture) if cache is not None else None

    def references_for_urlkey(
        self,
        urlkey: str,
    ) -> dict[str, RevisitReference]:
        return dict(self._references_by_urlkey.get(urlkey, {}))

def timestamp_to_warc_date(timestamp: datetime) -> str:
    """Normalize an aware timestamp to second-precision WARC UTC form."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    normalized = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_new_warc(
    path: Path,
    warc_filename: Optional[str] = None,
) -> tuple[BinaryIO, WARCWriter]:
    """Exclusively create a WARC and write its initial warcinfo record."""

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("xb")
    try:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        warcinfo = writer.create_warcinfo_record(
            warc_filename or path.name,
            {
                "software": "archive-magic-fetch 0.1.0",
                "format": "WARC File Format 1.0",
            },
        )
        writer.write_record(warcinfo)
    except Exception:
        stream.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise

    return stream, writer


def validate_warc(path: Path) -> None:
    """Require one fully parseable WARC with valid available digests."""

    record_types = []
    with path.open("rb") as stream:
        iterator = ArchiveIterator(stream, check_digests="raise")
        for record in iterator:
            record_types.append(record.rec_type)
            record.raw_stream.read()
        if iterator.err_count:
            raise ValueError(
                f"WARC contains {iterator.err_count} malformed "
                "record boundary warning(s)"
            )
    if not record_types or record_types[0] != "warcinfo":
        raise ValueError("WARC is missing its initial warcinfo record")
    if not any(
        record_type in {"response", "revisit"}
        for record_type in record_types
    ):
        raise ValueError("WARC contains no response or revisit records")


def write_response(writer: WARCWriter, record) -> None:
    """Write one complete response record."""

    writer.write_record(record)


@dataclass(frozen=True)
class RevisitReference:
    """Metadata needed to refer to one full response payload."""

    record_id: str
    target_uri: str
    warc_date: str
    payload_digest: str
    status_code: int


def response_reference(record) -> RevisitReference:
    """Extract the stable reference metadata from a response record."""

    record_id = record.rec_headers.get_header("WARC-Record-ID")
    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    warc_date = record.rec_headers.get_header("WARC-Date")
    payload_digest = record.rec_headers.get_header("WARC-Payload-Digest")
    status_text = (
        record.http_headers.get_statuscode()
        if record.http_headers is not None
        else None
    )
    if not all((record_id, target_uri, warc_date, payload_digest)):
        raise ValueError("response is missing revisit reference metadata")
    if status_text is None or not status_text.isdigit():
        raise ValueError("response is missing a numeric HTTP status")
    return RevisitReference(
        record_id=record_id,
        target_uri=target_uri,
        warc_date=warc_date,
        payload_digest=payload_digest,
        status_code=int(status_text),
    )


def _status_line(status_code: int) -> str:
    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def write_revisit(
    writer: WARCWriter,
    *,
    target_uri: str,
    capture_date: datetime,
    source_uri: str,
    mimetype: Optional[str],
    status_code: Optional[int],
    reference: RevisitReference,
):
    """Write one identical-payload-digest revisit record."""

    actual_status = status_code or reference.status_code
    headers = []
    if mimetype and mimetype != "-":
        headers.append(("Content-Type", mimetype))
    http_headers = StatusAndHeaders(
        _status_line(actual_status),
        headers,
        protocol="HTTP/1.1",
    )
    record = writer.create_revisit_record(
        target_uri,
        reference.payload_digest,
        reference.target_uri,
        reference.warc_date,
        http_headers=http_headers,
        warc_headers_dict={
            "WARC-Date": timestamp_to_warc_date(capture_date),
            "WARC-Source-URI": source_uri,
            "WARC-Refers-To": reference.record_id,
        },
    )
    writer.write_record(record)
    return record
