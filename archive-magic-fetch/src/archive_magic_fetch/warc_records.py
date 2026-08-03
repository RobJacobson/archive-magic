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


@dataclass(frozen=True)
class CachedWarcResponse:
    """One validated semantic response loaded from an existing WARC."""

    body: bytes
    status_code: int
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _CachedResponseLocation:
    offset: int
    length: int
    status_code: int


class ExistingWarcCache:
    """Lazy exact-response cache backed by one existing WARC."""

    def __init__(
        self,
        path: Path,
        entries: dict[
            tuple[str, str, str],
            Optional[_CachedResponseLocation],
        ],
    ) -> None:
        self.path = path
        self._entries = entries

    @classmethod
    def inventory(cls, path: Path) -> ExistingWarcCache:
        """Scan a WARC once and index unambiguous full responses."""

        entries: dict[
            tuple[str, str, str],
            Optional[_CachedResponseLocation],
        ] = {}
        with path.open("rb") as stream:
            iterator = ArchiveIterator(stream)
            for record in iterator:
                if record.rec_type != "response":
                    continue
                target_uri = record.rec_headers.get_header(
                    "WARC-Target-URI"
                )
                warc_date = record.rec_headers.get_header("WARC-Date")
                source_uri = record.rec_headers.get_header(
                    "WARC-Source-URI"
                )
                status_text = (
                    record.http_headers.get_statuscode()
                    if record.http_headers is not None
                    else None
                )
                offset = iterator.get_record_offset()
                length = iterator.get_record_length()
                if (
                    not all((target_uri, warc_date, source_uri))
                    or status_text is None
                    or not status_text.isdigit()
                ):
                    continue
                key = (target_uri, warc_date, source_uri)
                location = _CachedResponseLocation(
                    offset=offset,
                    length=length,
                    status_code=int(status_text),
                )
                if key in entries:
                    entries[key] = None
                else:
                    entries[key] = location
            if iterator.err_count:
                raise ValueError(
                    f"existing WARC contains {iterator.err_count} "
                    "malformed record boundary warning(s)"
                )
        return cls(path, entries)

    def get(self, capture) -> Optional[CachedWarcResponse]:
        """Load and validate an exact full response for one CDX capture."""

        key = (
            capture.original,
            timestamp_to_warc_date(capture.timestamp),
            capture.raw_url,
        )
        location = self._entries.get(key)
        if location is None:
            return None
        if (
            capture.statuscode is not None
            and location.status_code != capture.statuscode
        ):
            return None

        try:
            with self.path.open("rb") as stream:
                stream.seek(location.offset)
                member = stream.read(location.length)
            if len(member) != location.length:
                raise ValueError("compressed WARC member is truncated")

            iterator = ArchiveIterator(
                BytesIO(member),
                check_digests="raise",
            )
            record = next(iterator)
            if record.rec_type != "response":
                raise ValueError("cached member is not a response")
            target_uri = record.rec_headers.get_header("WARC-Target-URI")
            warc_date = record.rec_headers.get_header("WARC-Date")
            source_uri = record.rec_headers.get_header("WARC-Source-URI")
            payload_digest = record.rec_headers.get_header(
                "WARC-Payload-Digest"
            )
            status_text = (
                record.http_headers.get_statuscode()
                if record.http_headers is not None
                else None
            )
            if (target_uri, warc_date, source_uri) != key:
                raise ValueError("cached response identity changed")
            if not payload_digest:
                raise ValueError("cached response has no payload digest")
            if status_text is None or not status_text.isdigit():
                raise ValueError("cached response has no numeric status")
            status_code = int(status_text)
            if status_code != location.status_code:
                raise ValueError("cached response status changed")
            if (
                capture.statuscode is not None
                and status_code != capture.statuscode
            ):
                return None
            headers = tuple(record.http_headers.headers)
            body = record.content_stream().read()
            try:
                next(iterator)
            except StopIteration:
                pass
            else:
                raise ValueError("cached member contains multiple records")
            if iterator.err_count:
                raise ValueError("cached member has a malformed boundary")
            return CachedWarcResponse(
                body=body,
                status_code=status_code,
                headers=headers,
            )
        except Exception as error:
            raise ValueError(
                f"cannot reuse response from {self.path}: {error}"
            ) from error


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
