"""Canonical identities shared by CDX rows and WARC records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from surt import surt


_VALID_SHA1 = re.compile(r"[A-Z2-7]{32}")


@dataclass(frozen=True, order=True)
class CaptureIdentity:
    """One logical archived HTTP capture."""

    urlkey: str
    timestamp: str
    status_code: Optional[int]
    payload_digest: Optional[str]


def normalize_payload_digest(digest: object) -> Optional[str]:
    """Return a canonical WARC/CDX SHA-1 digest when valid."""

    if not isinstance(digest, str):
        return None
    value = digest.strip().upper()
    if value.startswith("SHA1:"):
        value = value[5:]
    if not _VALID_SHA1.fullmatch(value):
        return None
    return f"sha1:{value}"


def normalized_urlkey(url: str) -> str:
    """Return the same canonical SURT key used by the replay index."""

    if not isinstance(url, str) or not url:
        raise ValueError("capture URL must be a non-empty string")
    try:
        return surt(url)
    except Exception as error:
        raise ValueError(f"cannot normalize capture URL {url!r}") from error


def timestamp_to_cdx(timestamp: datetime) -> str:
    """Normalize an aware datetime to a second-precision CDX timestamp."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def warc_date_to_cdx(value: str) -> str:
    """Normalize an ISO WARC-Date to a second-precision CDX timestamp."""

    if not isinstance(value, str) or not value:
        raise ValueError("WARC-Date must be a non-empty string")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid WARC-Date: {value!r}") from error
    return timestamp_to_cdx(timestamp)


def identity_for_capture(capture) -> CaptureIdentity:
    """Build a logical identity from an Internet Archive CDX row."""

    status_code = capture.statuscode
    if status_code is not None and not isinstance(status_code, int):
        raise ValueError("CDX status must be an integer or null")
    return CaptureIdentity(
        urlkey=normalized_urlkey(capture.original),
        timestamp=timestamp_to_cdx(capture.timestamp),
        status_code=status_code,
        payload_digest=normalize_payload_digest(capture.digest),
    )


def identity_for_warc_record(record) -> CaptureIdentity:
    """Build a logical identity from one response or revisit record."""

    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    warc_date = record.rec_headers.get_header("WARC-Date")
    digest = record.rec_headers.get_header("WARC-Payload-Digest")
    status_text = (
        record.http_headers.get_statuscode()
        if record.http_headers is not None
        else None
    )
    if not target_uri or not warc_date:
        raise ValueError("WARC record is missing target URI or date")
    if status_text is None or not status_text.isdigit():
        raise ValueError("WARC record is missing a numeric HTTP status")
    normalized_digest = normalize_payload_digest(digest)
    if normalized_digest is None:
        raise ValueError("WARC record is missing a valid payload digest")
    return CaptureIdentity(
        urlkey=normalized_urlkey(target_uri),
        timestamp=warc_date_to_cdx(warc_date),
        status_code=int(status_text),
        payload_digest=normalized_digest,
    )
