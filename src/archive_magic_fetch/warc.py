"""WARC 1.0 record preparation and writing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from warcio.warcwriter import WARCWriter


@dataclass(frozen=True)
class CanonicalResponse:
    """Reference to the first verified response for one payload digest."""

    record_id: str
    capture_date: str


def cdx_timestamp_to_warc_date(timestamp: str) -> str:
    """Convert a full CDX timestamp to the WARC 1.0 UTC representation."""

    parsed = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare_response(record, url: str, timestamp: str):
    """Replace synthesized capture identity with authoritative CDX identity."""

    record.rec_headers.replace_header("WARC-Target-URI", url)
    record.rec_headers.replace_header(
        "WARC-Date", cdx_timestamp_to_warc_date(timestamp)
    )
    return record


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
    capture_date = record.rec_headers.get_header("WARC-Date")
    if not record_id or not capture_date:
        raise ValueError("response is missing required WARC identity headers")

    writer.write_record(record)
    return CanonicalResponse(record_id=record_id, capture_date=capture_date)


def write_revisit(
    writer: WARCWriter,
    url: str,
    timestamp: str,
    digest: str,
    canonical: CanonicalResponse,
) -> None:
    """Write an identical-payload-digest revisit referencing its response."""

    revisit = writer.create_revisit_record(
        url,
        digest=digest,
        refers_to_uri=url,
        refers_to_date=canonical.capture_date,
        warc_headers_dict={
            "WARC-Date": cdx_timestamp_to_warc_date(timestamp),
        },
    )
    revisit.rec_headers.add_header("WARC-Refers-To", canonical.record_id)
    writer.write_record(revisit)

