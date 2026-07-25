"""WARC 1.0 record preparation and writing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

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
