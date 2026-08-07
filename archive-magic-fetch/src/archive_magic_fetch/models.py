"""Immutable types, capture identity, and named policy constants."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from surt import surt

# ---------------------------------------------------------------------------
# Policy constants (not CLI options)
# ---------------------------------------------------------------------------

DEFAULT_OUTPUT_ROOT = Path("../archives")
WARC_TARGET_BYTES = 1_000_000_000
PLAYBACK_REQUESTS_PER_SECOND = 8.0
# Bounded concurrency separate from start rate. 24 sustains 8 starts/s for
# typical archive responses without excessive open connections; re-measure
# on large collections before changing.
MAX_IN_FLIGHT = 16
MAX_PLAYBACK_ATTEMPTS = 9  # first try + 8 retries
MAX_RETRY_DELAY_S = 3600
DEFAULT_429_COOLDOWN_S = 60.0
MAX_429_COOLDOWN_S = 900.0
RESULT_QUEUE_SIZE = 64
CDX_PAGE_LIMIT = 10_000
DEFAULT_DATE_START = "19950101000000"
COLLECTION_SCHEMA_VERSION = 1
FAILURES_SCHEMA_VERSION = 1
WARC_VERSION = "1.1"
SOFTWARE_ID = "archive-magic-fetch/0.1.0"
USER_AGENT = (
    "archive-magic-fetch/0.1.0 "
    "(+https://github.com/RobJacobson/archive-magic)"
)

CDX_PAYLOAD_DIGEST_HEADER = "CDX-Payload-Digest"
CDX_STATUS_HEADER = "CDX-Status"
CDX_URLKEY_HEADER = "CDX-Urlkey"
MISSING_CDX_PAYLOAD_DIGEST = "-"
MISSING_CDX_STATUS = "-"

_VALID_SHA1 = re.compile(r"[A-Z2-7]{32}")
_CDX_TIMESTAMP = re.compile(r"^\d{14}$")


class FailureCategory(str, Enum):
    """Stable failure categories in failures.json."""

    MALFORMED_CDX = "malformed_cdx"
    BLOCKED = "blocked"
    EXACT_MISMATCH = "exact_mismatch"
    UNAVAILABLE = "unavailable"
    RETRY_EXHAUSTED = "retry_exhausted"
    TRUNCATED = "truncated"
    DIGEST_VALIDATION = "digest_validation"
    PUBLICATION = "publication"


@dataclass(frozen=True, order=True)
class CaptureIdentity:
    """One logical Internet Archive capture.

    Identity preserves raw CDX fields so statusless and digestless rows remain
    distinct from numeric equivalents after playback.
    """

    urlkey: str
    original_url: str
    timestamp: str
    status_token: str
    payload_digest: str

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.timestamp,
            self.urlkey,
            self.original_url,
            self.status_token,
            self.payload_digest,
        )


@dataclass(frozen=True)
class ParsedCapture:
    """One validated CDX row ready for selection/scheduling."""

    identity: CaptureIdentity
    year: int
    is_redirect: bool
    has_usable_digest: bool
    mime: str
    raw_line: str


@dataclass(frozen=True)
class PlaybackResult:
    """Validated exact-playback payload ready for WARC writing."""

    identity: CaptureIdentity
    body: bytes
    status_code: int
    headers: tuple[tuple[str, str], ...]
    warc_date: str
    source_uri: str
    warc_payload_digest: str


@dataclass(frozen=True)
class RevisitResult:
    """A same-year revisit of an already-stored full response."""

    identity: CaptureIdentity
    warc_date: str
    refers_to_target_uri: str
    refers_to_date: str
    warc_payload_digest: str
    http_status_code: int
    http_headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class UnresolvedFailure:
    """One unresolved capture failure for the failure ledger."""

    identity: CaptureIdentity
    category: FailureCategory
    message: str


@dataclass(frozen=True)
class WarcArtifact:
    """One finalized WARC shard."""

    relative_key: str
    year: int
    sequence: int
    path: Path
    size_bytes: int
    sha256: str
    record_count: int


@dataclass(frozen=True)
class IndexArtifact:
    """One finalized CDXJ index file."""

    relative_key: str
    path: Path
    size_bytes: int
    sha256: str
    capture_count: int


@dataclass
class RunMetrics:
    """Aggregate telemetry for one fetch run."""

    cdx_requests: int = 0
    cdx_duration_s: float = 0.0
    playback_starts: int = 0
    playback_completions: int = 0
    playback_bytes: int = 0
    peak_in_flight: int = 0
    rate_gate_wait_s: float = 0.0
    cooldown_wait_s: float = 0.0
    local_reuses: int = 0
    downloads: int = 0
    revisits: int = 0
    selected: int = 0
    represented: int = 0
    unresolved: int = 0
    warc_write_s: float = 0.0
    index_s: float = 0.0
    attempts_by_category: dict[str, int] = field(default_factory=dict)

    def bump_attempt(self, category: str) -> None:
        self.attempts_by_category[category] = (
            self.attempts_by_category.get(category, 0) + 1
        )


def normalize_payload_digest(digest: object) -> Optional[str]:
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
    if not text or text == "-":
        return MISSING_CDX_STATUS
    return text


def normalized_urlkey(url: str) -> str:
    """Return the SURT urlkey used for CDXJ and identity."""

    if not isinstance(url, str) or not url:
        raise ValueError("capture URL must be a non-empty string")
    try:
        return surt(url)
    except Exception as error:
        raise ValueError(f"cannot normalize capture URL {url!r}") from error


_DEFAULT_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
}


def normalize_original_url(url: str) -> str:
    """Strip scheme-default ports from a capture URL; leave other spelling intact.

    ``http://host:80/path`` and ``https://host:443/path`` become
    ``http://host/path`` and ``https://host/path``. Non-default ports are kept.
    """

    if not isinstance(url, str) or not url:
        raise ValueError("capture URL must be a non-empty string")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    default_port = _DEFAULT_PORTS.get(scheme)
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
        netloc = f"{userinfo}@{hostport[: -len(suffix)]}"
    else:
        if not netloc.endswith(suffix):
            return url
        netloc = netloc[: -len(suffix)]
    return urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def timestamp_to_cdx(timestamp: datetime) -> str:
    """Normalize an aware datetime to a 14-digit CDX timestamp."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def warc_date_to_cdx(value: str) -> str:
    """Normalize an ISO WARC-Date to a 14-digit CDX timestamp."""

    if not isinstance(value, str) or not value:
        raise ValueError("WARC-Date must be a non-empty string")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"invalid WARC-Date: {value!r}") from error
    return timestamp_to_cdx(timestamp)


def timestamp_to_warc_date(timestamp: datetime) -> str:
    """Normalize an aware timestamp to second-precision WARC UTC form."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    normalized = timestamp.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def cdx_timestamp_to_warc_date(timestamp: str) -> str:
    """Convert a 14-digit CDX timestamp to WARC-Date form."""

    if not _CDX_TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    return (
        f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}T"
        f"{timestamp[8:10]}:{timestamp[10:12]}:{timestamp[12:14]}Z"
    )


def timestamp_year(timestamp: str) -> int:
    """Return the UTC calendar year of a 14-digit CDX timestamp."""

    if not _CDX_TIMESTAMP.fullmatch(timestamp):
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    return int(timestamp[0:4])


def is_redirect_status_token(token: str) -> bool:
    """Return whether a status token is a known 3xx redirect."""

    if not token.isdigit():
        return False
    code = int(token)
    return 300 <= code < 400


def make_identity(
    *,
    original_url: str,
    timestamp: str,
    status_token: str,
    payload_digest: str,
    urlkey: Optional[str] = None,
) -> CaptureIdentity:
    """Build one capture identity from raw field values."""

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
    """Serialize one capture identity for JSON artifacts."""

    return {
        "urlkey": identity.urlkey,
        "original_url": identity.original_url,
        "timestamp": identity.timestamp,
        "status_token": identity.status_token,
        "payload_digest": identity.payload_digest,
    }


def identity_from_dict(data: dict[str, object]) -> CaptureIdentity:
    """Deserialize one capture identity from a JSON object."""

    return CaptureIdentity(
        urlkey=str(data["urlkey"]),
        original_url=normalize_original_url(str(data["original_url"])),
        timestamp=str(data["timestamp"]),
        status_token=str(data["status_token"]),
        payload_digest=str(data["payload_digest"]),
    )


def current_utc_cdx_timestamp() -> str:
    """Return the current UTC time as a full CDX timestamp."""

    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def current_run_id() -> str:
    """Return one UTC run identifier suitable for sources/ directory names.

    Uses microsecond precision so rapid consecutive runs do not collide.
    """

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
