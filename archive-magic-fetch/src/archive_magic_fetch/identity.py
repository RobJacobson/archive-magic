"""Capture identity and URL, timestamp, status, and digest normalization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

from surt import surt

from .models import CaptureIdentity
from .protocol import (
    EMPTY_PAYLOAD_DIGEST,
    INVALID_URI_PAYLOAD_DIGEST,
    MISSING_CDX_PAYLOAD_DIGEST,
    MISSING_CDX_STATUS,
)


_VALID_SHA1 = re.compile(r"[A-Z2-7]{32}")
_CDX_TIMESTAMP = re.compile(r"^\d{14}$")
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def normalize_payload_digest(digest: object) -> str | None:
    """Return a canonical ``sha1:…`` digest when the token is valid."""

    if not isinstance(digest, str):
        return None
    value = digest.strip().upper()
    if value.startswith("SHA1:"):
        value = value[5:]
    if not _VALID_SHA1.fullmatch(value):
        return None
    return f"sha1:{value}"


def cdx_payload_digest_token(digest: object) -> str:
    """Return the durable identity token for a CDX payload digest field."""

    return normalize_payload_digest(digest) or MISSING_CDX_PAYLOAD_DIGEST


def cdx_status_token(status: object) -> str:
    """Return the durable identity token for a CDX status field."""

    if status is None:
        return MISSING_CDX_STATUS
    if isinstance(status, int):
        return str(status)
    text = str(status).strip()
    return text if text and text != "-" else MISSING_CDX_STATUS


def normalized_urlkey(url: str) -> str:
    """Return the SURT urlkey used for CDXJ and identity."""

    if not isinstance(url, str) or not url:
        raise ValueError("capture URL must be a non-empty string")
    try:
        return surt(url)
    except Exception as error:
        raise ValueError(f"cannot normalize capture URL {url!r}") from error


def is_invalid_uri_payload_digest(digest: object) -> bool:
    return normalize_payload_digest(digest) == INVALID_URI_PAYLOAD_DIGEST


def is_empty_payload_digest(digest: object) -> bool:
    return normalize_payload_digest(digest) == EMPTY_PAYLOAD_DIGEST


def wayback_url(timestamp: str, original_url: str) -> str:
    return f"https://web.archive.org/web/{timestamp}id_/{original_url}"


def normalize_original_url(url: str) -> str:
    """Strip scheme-default ports from a capture URL."""

    if not isinstance(url, str) or not url:
        raise ValueError("capture URL must be a non-empty string")
    parts = urlsplit(url)
    default_port = _DEFAULT_PORTS.get(parts.scheme.lower())
    if default_port is None or not parts.netloc:
        return url
    try:
        port = parts.port
    except ValueError:
        return url
    if port != default_port:
        return url

    suffix = f":{default_port}"
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        if not hostport.endswith(suffix):
            return url
        netloc = f"{userinfo}@{hostport[:-len(suffix)]}"
    else:
        if not netloc.endswith(suffix):
            return url
        netloc = netloc[:-len(suffix)]
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _fully_unquote(value: str) -> str:
    previous = None
    current = value
    while previous != current:
        previous = current
        current = unquote(current)
    return current


def same_original_url(left: str, right: str) -> bool:
    """Compare original URLs while tolerating IA percent-encoding variants."""

    a = normalize_original_url(left)
    b = normalize_original_url(right)
    if a == b:
        return True
    a_parts = urlsplit(a)
    b_parts = urlsplit(b)
    if (a_parts.scheme, a_parts.netloc) != (b_parts.scheme, b_parts.netloc):
        return False
    a_resource = tuple(
        map(_fully_unquote, (a_parts.path, a_parts.query, a_parts.fragment))
    )
    b_resource = tuple(
        map(_fully_unquote, (b_parts.path, b_parts.query, b_parts.fragment))
    )
    return a_resource == b_resource


def timestamp_to_cdx(timestamp: datetime) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def warc_date_to_cdx(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("WARC-Date must be a non-empty string")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid WARC-Date: {value!r}") from error
    return timestamp_to_cdx(timestamp)


def timestamp_to_warc_date(timestamp: datetime) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    normalized = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def cdx_timestamp_to_warc_date(timestamp: str) -> str:
    if not _CDX_TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    return (
        f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}T"
        f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}Z"
    )


def timestamp_year(timestamp: str) -> int:
    if not _CDX_TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    return int(timestamp[:4])


def is_redirect_status_token(token: str) -> bool:
    return token.isdigit() and 300 <= int(token) < 400


def revisit_group_key(identity: CaptureIdentity) -> tuple[str, str, str] | None:
    """Return the representative-map key for an identical-payload revisit.

    Missing digests cannot group. Empty payloads keep status in the key so a
    301 and a 302 stay distinct. Any other digest already identifies the
    bytes, so CDX status is ignored: IA ``warc/revisit`` rows use ``-``.
    """

    if identity.payload_digest == MISSING_CDX_PAYLOAD_DIGEST:
        return None
    status = (
        identity.status_token
        if is_empty_payload_digest(identity.payload_digest)
        else ""
    )
    return identity.urlkey, identity.payload_digest, status


def make_identity(
    *,
    original_url: str,
    timestamp: str,
    status_token: str,
    payload_digest: str,
    urlkey: str | None = None,
) -> CaptureIdentity:
    if not _CDX_TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    canonical_url = normalize_original_url(original_url)
    return CaptureIdentity(
        urlkey=urlkey or normalized_urlkey(canonical_url),
        original_url=canonical_url,
        timestamp=timestamp,
        status_token=cdx_status_token(status_token),
        payload_digest=cdx_payload_digest_token(payload_digest),
    )


def identity_to_dict(identity: CaptureIdentity) -> dict[str, str]:
    return {
        "urlkey": identity.urlkey,
        "original_url": identity.original_url,
        "timestamp": identity.timestamp,
        "status_token": identity.status_token,
        "payload_digest": identity.payload_digest,
    }


def current_utc_cdx_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def current_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
