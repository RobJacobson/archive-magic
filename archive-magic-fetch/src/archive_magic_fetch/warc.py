"""WARC 1.0 record preparation and writing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import BinaryIO, Optional

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


def timestamp_to_warc_date(timestamp: datetime) -> str:
    """Normalize an aware timestamp to second-precision WARC UTC form."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    normalized = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def open_new_warc(path: Path) -> tuple[BinaryIO, WARCWriter]:
    """Exclusively create a WARC and write its initial warcinfo record."""

    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("xb")
    try:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        warcinfo = writer.create_warcinfo_record(
            path.name,
            {
                "software": "archive-magic-fetch 0.1.0",
                "format": "WARC File Format 1.0",
            },
        )
        writer.write_record(warcinfo)
    except Exception:
        stream.close()
        raise

    return stream, writer


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
