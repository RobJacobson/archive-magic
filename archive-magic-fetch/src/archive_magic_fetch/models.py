"""Source-neutral capture, resolution, artifact, and metrics records."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FailureCategory(str, Enum):
    BLOCKED = "blocked"
    EXACT_MISMATCH = "exact_mismatch"
    UNAVAILABLE = "unavailable"
    RETRY_EXHAUSTED = "retry_exhausted"
    TRUNCATED = "truncated"


@dataclass(frozen=True, order=True)
class CaptureIdentity:
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
class CommonCrawlLocator:
    crawl_id: str
    filename: str
    offset: int
    length: int

    def __post_init__(self) -> None:
        if not isinstance(self.crawl_id, str) or not re.fullmatch(r"CC-MAIN-[0-9-]+", self.crawl_id):
            raise ValueError("invalid Common Crawl collection ID")
        if (not isinstance(self.filename, str)
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", self.filename)
                or self.filename.startswith("/")
                or any(part in ("", ".", "..") for part in self.filename.split("/"))):
            raise ValueError("unsafe Common Crawl filename")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("Common Crawl offset must be nonnegative")
        if type(self.length) is not int or self.length <= 0:
            raise ValueError("Common Crawl length must be positive")


@dataclass(frozen=True)
class CaptureRef:
    identity: CaptureIdentity
    mime: str
    locator: CommonCrawlLocator | None = None


@dataclass(frozen=True)
class CaptureResult:
    identity: CaptureIdentity
    body: bytes
    status_code: int
    headers: tuple[tuple[str, str], ...]
    warc_date: str
    source_uri: str
    warc_payload_digest: str
    digest_matched: bool = True


@dataclass(frozen=True)
class RevisitResult:
    identity: CaptureIdentity
    warc_date: str
    refers_to_target_uri: str
    refers_to_date: str
    warc_payload_digest: str
    http_status_code: int


@dataclass(frozen=True)
class UnresolvedFailure:
    identity: CaptureIdentity
    category: FailureCategory
    message: str
    display_reason: str | None = None


@dataclass(frozen=True)
class WarcArtifact:
    relative_key: str
    collection_id: str
    sequence: int
    path: Path
    size_bytes: int
    sha256: str
    record_count: int


@dataclass(frozen=True)
class IndexArtifact:
    relative_key: str
    path: Path
    size_bytes: int
    sha256: str
    capture_count: int


@dataclass
class RunMetrics:
    cdx_duration_s: float = 0.0
    playback_attempts: int = 0
    playback_bytes: int = 0
    local_reuses: int = 0
    payload_reuses: int = 0
    downloads: int = 0
    revisits: int = 0
    digest_mismatch_accepted: int = 0
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


class CaptureKind(str, Enum):
    EXISTING = "existing"
    REVISIT = "revisit"
    DOWNLOADED = "downloaded"
    FAILURE = "failure"


@dataclass(frozen=True)
class CaptureOutcome:
    capture: CaptureRef
    kind: CaptureKind
    playback: CaptureResult | None = None
    representative: StoredResponse | None = None
    failure: UnresolvedFailure | None = None
    attempts: int = 0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class UrlOutcome:
    url: str
    captures: tuple[CaptureOutcome, ...]
    attempts: int
    playback_bytes: int
    categories: tuple[str, ...]


@dataclass(frozen=True)
class DownloadOutcome:
    result: CaptureResult | None
    failure: UnresolvedFailure | None
    attempts: int
    elapsed_s: float
    categories: tuple[str, ...]


@dataclass(frozen=True)
class CaptureListing:
    captures: tuple[CaptureRef, ...]
    query: dict[str, object]


class PublicationError(RuntimeError):
    """The local archive was retained, but its remote mirror is incomplete."""


@dataclass(frozen=True)
class StoredResponse:
    """Compact revisit reference for one full response.

    Never retain payload bytes or HTTP headers; pywb resolves them from the
    referenced full response.
    """

    identity: CaptureIdentity
    warc_date: str
    warc_payload_digest: str
    target_uri: str
    status_code: int
