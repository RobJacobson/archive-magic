"""WARC 1.0 record preparation and writing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from warcio.warcwriter import WARCWriter


@dataclass(frozen=True)
class CanonicalResponse:
    """Reference to the first response for one payload/status signature."""

    record_id: str
    target_uri: str
    capture_date: str


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


def write_response(writer: WARCWriter, record) -> CanonicalResponse:
    """Write a response and return the reference needed by later revisits."""

    record_id = record.rec_headers.get_header("WARC-Record-ID")
    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    capture_date = record.rec_headers.get_header("WARC-Date")
    if not record_id or not target_uri or not capture_date:
        raise ValueError("response is missing required WARC identity headers")

    writer.write_record(record)
    return CanonicalResponse(
        record_id=record_id,
        target_uri=target_uri,
        capture_date=capture_date,
    )


def write_revisit(
    writer: WARCWriter,
    url: str,
    capture_date: str,
    digest: str,
    canonical: CanonicalResponse,
) -> None:
    """Write an identical-payload-digest revisit referencing its response."""

    revisit = writer.create_revisit_record(
        url,
        digest=digest,
        refers_to_uri=canonical.target_uri,
        refers_to_date=canonical.capture_date,
        warc_headers_dict={
            "WARC-Date": capture_date,
        },
    )
    revisit.rec_headers.add_header("WARC-Refers-To", canonical.record_id)
    writer.write_record(revisit)
