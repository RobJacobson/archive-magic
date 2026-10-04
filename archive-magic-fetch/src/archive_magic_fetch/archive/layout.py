"""Portable archive paths, filenames, and artifact inventories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from archive_magic_fetch.models import IndexArtifact, WarcArtifact
from archive_magic_fetch.runtime.filesystem import file_sha256

_WWW_ALIAS_PREFIX = re.compile(r"^www\d*\.")


_TEMP_NAME = re.compile(r"^\.tmp-|^.*\.tmp$")


_COLLECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


_LEGACY_NAMES = (
    "archive",
    "sources",
    "index.cdxj",
    "collection.json",
    "failures.json",
    "collections",
    "captures",
)


@dataclass(frozen=True)
class ArchiveLayout:
    """Filesystem boundaries for one domain archive and its collections."""

    root: Path
    archive_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def logs_root(self) -> Path:
        return self.root.parent / "logs"

    def validate_collection_id(self, collection_id: str) -> str:
        if not _COLLECTION_ID.fullmatch(collection_id) or collection_id in {".", ".."}:
            raise ValueError(f"unsafe collection ID: {collection_id!r}")
        return collection_id

    def validate_run_id(self, run_id: str) -> str:
        if not _COLLECTION_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ValueError(f"unsafe run ID: {run_id!r}")
        return run_id

    def collection_dir(self, collection_id: str) -> Path:
        self.validate_collection_id(collection_id)
        return self.root

    def run_record(self, run_id: str) -> Path:
        return self.logs_root / f"{self.validate_run_id(run_id)}.json"

    def run_log(self, run_id: str) -> Path:
        return self.logs_root / f"{self.validate_run_id(run_id)}.log"

    def index_filename(self, collection_id: str) -> str:
        collection_id = self.validate_collection_id(collection_id)
        return f"{self.archive_id}-{collection_id}-index.cdxj"

    def collection_index(self, collection_id: str) -> Path:
        return self.collection_dir(collection_id) / self.index_filename(collection_id)

    def collection_warc_filename(self, collection_id: str, sequence: int) -> str:
        collection_id = self.validate_collection_id(collection_id)
        if sequence < 1:
            raise ValueError(f"WARC sequence must be positive, got {sequence}")
        return f"{self.archive_id}-{collection_id}-{sequence:03d}.warc.gz"

    def collection_warc_path(self, collection_id: str, sequence: int) -> Path:
        return self.collection_dir(collection_id) / self.collection_warc_filename(
            collection_id, sequence
        )


def normalize_domain(
    value: str,
    *,
    allow_bare: bool = False,
) -> tuple[str, Optional[int]]:
    """Return normalized host and significant port for one URL pattern."""

    text = value.strip()
    has_scheme = "://" in text
    parsed = urlsplit(text if has_scheme else f"//{text}")
    if not allow_bare and (not parsed.scheme or not parsed.netloc):
        raise ValueError(f"URL must be absolute: {value}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL user information is not supported")

    host = parsed.hostname
    if not host:
        raise ValueError(f"URL must include a host: {value}")
    host = host.rstrip(".").lower()
    if not host:
        raise ValueError("URL host cannot be empty")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError(f"URL host is not valid IDNA: {host}") from error
    host = _WWW_ALIAS_PREFIX.sub("", host, count=1)
    if not host:
        raise ValueError("URL host cannot be only www")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"URL has an invalid port: {value}") from error
    scheme = parsed.scheme.lower()
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    return host, port


def normalize_archive_id(url_pattern: str) -> str:
    """Derive one safe domain-archive directory name from a URL pattern."""

    pattern = url_pattern.strip()
    if pattern.startswith("*."):
        pattern = pattern[2:]
    host, port = normalize_domain(pattern, allow_bare=True)
    if "*" in host:
        raise ValueError(
            f"URL pattern must identify one unambiguous website host: {url_pattern}"
        )
    name = host if port is None else f"{host}%3A{port}"
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"URL pattern produced an unsafe archive name: {name}")
    return name


def reject_legacy_layout(layout: ArchiveLayout) -> None:
    """Reject pre-flat Archive Magic output rather than mixing schemas."""

    found = [name for name in _LEGACY_NAMES if (layout.root / name).exists()]
    if found:
        raise ValueError(
            "unsupported legacy archive layout; delete and regenerate the archive "
            f"(found: {', '.join(found)})"
        )


def ensure_collection_dirs(layout: ArchiveLayout) -> None:
    """Create the data and log directories."""

    layout.root.mkdir(parents=True, exist_ok=True)
    layout.logs_root.mkdir(parents=True, exist_ok=True)


def cleanup_temps(layout: ArchiveLayout) -> None:
    """Remove abandoned short-lived temporary and legacy partial files."""

    if not layout.root.is_dir():
        return
    for path in layout.root.rglob("*"):
        if not path.is_file():
            continue
        if _TEMP_NAME.match(path.name) or path.name.endswith(".warc.gz.partial"):
            try:
                path.unlink()
            except OSError:
                pass


def _collection_warc_name_pattern(
    layout: ArchiveLayout, collection_id: str
) -> re.Pattern[str]:
    """Return the basename pattern for one portable collection's WARC shards."""

    return re.compile(
        rf"{re.escape(layout.archive_id)}-{re.escape(collection_id)}-"
        r"(?P<seq>\d{3,})\.warc\.gz"
    )


def reset_collection_data(layout: ArchiveLayout, collection_id: str) -> None:
    """Remove WARC, legacy partial, and CDXJ artifacts for one collection."""

    collection_id = layout.validate_collection_id(collection_id)
    for path in list_collection_warcs(layout, collection_id):
        path.unlink()
    collection_dir = layout.collection_dir(collection_id)
    if collection_dir.is_dir():
        for path in collection_dir.glob(
            f"{layout.archive_id}-{collection_id}-*.warc.gz.partial"
        ):
            path.unlink()
    index_path = layout.collection_index(collection_id)
    if index_path.is_file():
        index_path.unlink()


def list_collection_warcs(layout: ArchiveLayout, collection_id: str) -> list[Path]:
    """Return finalized WARC paths for one portable collection."""

    collection_id = layout.validate_collection_id(collection_id)
    collection_dir = layout.collection_dir(collection_id)
    if not collection_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    pattern = _collection_warc_name_pattern(layout, collection_id)
    for path in collection_dir.iterdir():
        if not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        found.append((int(match.group("seq")), path))
    found.sort(key=lambda item: item[0])
    return [path for _, path in found]


def last_collection_warc(
    layout: ArchiveLayout, collection_id: str
) -> tuple[int, Path] | None:
    """Return the highest-sequence finalized WARC, if any."""

    existing = list_collection_warcs(layout, collection_id)
    if not existing:
        return None
    last = existing[-1]
    match = _collection_warc_name_pattern(layout, collection_id).fullmatch(last.name)
    assert match is not None
    return int(match.group("seq")), last


def warc_artifact_from_path(
    layout: ArchiveLayout,
    path: Path,
    *,
    collection_id: str,
    record_count: int,
) -> WarcArtifact:
    """Build a WarcArtifact descriptor for a finalized WARC."""

    relative = path.relative_to(layout.root).as_posix()
    layout.validate_collection_id(collection_id)
    expected_parent = layout.collection_dir(collection_id)
    match = _collection_warc_name_pattern(layout, collection_id).fullmatch(path.name)
    if match is None or path.parent.resolve() != expected_parent.resolve():
        raise ValueError(f"unexpected WARC filename: {path.name}")
    return WarcArtifact(
        relative_key=relative,
        collection_id=collection_id,
        sequence=int(match.group("seq")),
        path=path,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
        record_count=record_count,
    )


def index_artifact_from_path(
    layout: ArchiveLayout,
    path: Path,
    *,
    capture_count: int | None = None,
) -> IndexArtifact:
    """Build an IndexArtifact descriptor for a CDXJ file."""

    if capture_count is None:
        capture_count = sum(
            1 for line in path.read_text(encoding="utf-8").splitlines() if line
        )
    return IndexArtifact(
        relative_key=path.relative_to(layout.root).as_posix(),
        path=path,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
        capture_count=capture_count,
    )
