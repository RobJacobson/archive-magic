"""Shared data models for acquisition, playback, and publication."""

from __future__ import annotations

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
class ParsedCapture:
    identity: CaptureIdentity
    mime: str


@dataclass(frozen=True)
class PlaybackResult:
    identity: CaptureIdentity
    body: bytes
    status_code: int
    headers: tuple[tuple[str, str], ...]
    warc_date: str
    source_uri: str
    warc_payload_digest: str
    digest_matched: bool = True
    substituted: bool = False


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
