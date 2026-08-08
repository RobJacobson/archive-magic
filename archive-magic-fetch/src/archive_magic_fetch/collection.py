"""Collection layout, atomic publication, manifest, and failure ledger."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlsplit

from .models import (
    COLLECTION_SCHEMA_VERSION,
    DEFAULT_OUTPUT_ROOT,
    FAILURES_SCHEMA_VERSION,
    WARC_TARGET_BYTES,
    WARC_VERSION,
    FailureCategory,
    IndexArtifact,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
    identity_from_dict,
    identity_to_dict,
)


_WWW_ALIAS_PREFIX = re.compile(r"^www\d*\.")
_TEMP_NAME = re.compile(r"^\.tmp-|^.*\.(tmp|partial)$")
_WARC_NAME = re.compile(
    r"^(?P<id>.+)-(?P<year>\d{4})-(?P<seq>\d{3})\.warc\.gz$"
)


@dataclass(frozen=True)
class CollectionLayout:
    """Filesystem boundaries for one website collection."""

    archives_root: Path
    collection_id: str

    @property
    def root(self) -> Path:
        return self.archives_root / self.collection_id

    @property
    def archive_root(self) -> Path:
        return self.root / "archive"

    @property
    def indexes_root(self) -> Path:
        return self.root / "indexes"

    @property
    def years_index_root(self) -> Path:
        return self.indexes_root / "years"

    @property
    def collection_index(self) -> Path:
        return self.indexes_root / "index.cdxj"

    @property
    def sources_root(self) -> Path:
        return self.root / "sources"

    @property
    def work_root(self) -> Path:
        return self.root / ".work"

    @property
    def manifest_path(self) -> Path:
        return self.root / "collection.json"

    @property
    def failures_path(self) -> Path:
        return self.root / "failures.json"

    def year_dir(self, year: int) -> Path:
        return self.archive_root / f"{year:04d}"

    def annual_index(self, year: int) -> Path:
        return self.years_index_root / f"{year:04d}.cdxj"

    def warc_filename(self, year: int, sequence: int) -> str:
        if sequence < 1 or sequence > 999:
            raise ValueError(
                f"WARC sequence must be 001-999, got {sequence}"
            )
        return f"{self.collection_id}-{year:04d}-{sequence:03d}.warc.gz"

    def warc_relative_key(self, year: int, sequence: int) -> str:
        return f"archive/{year:04d}/{self.warc_filename(year, sequence)}"

    def warc_path(self, year: int, sequence: int) -> Path:
        return self.year_dir(year) / self.warc_filename(year, sequence)

    def annual_index_relative_key(self, year: int) -> str:
        return f"indexes/years/{year:04d}.cdxj"

    def collection_index_relative_key(self) -> str:
        return "indexes/index.cdxj"


def normalize_domain(
    value: str,
    *,
    allow_bare: bool = False,
) -> tuple[str, Optional[int]]:
    """Return normalized host and significant port for one URL pattern."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL must be a non-empty string")
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
    if (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    ):
        port = None
    return host, port


def normalize_collection_id(url_pattern: str) -> str:
    """Derive one safe collection directory name from a URL pattern."""

    if not isinstance(url_pattern, str) or not url_pattern.strip():
        raise ValueError("URL pattern must be a non-empty string")
    pattern = url_pattern.strip()
    if pattern.startswith("*."):
        pattern = pattern[2:]
    host, port = normalize_domain(pattern, allow_bare=True)
    if "*" in host:
        raise ValueError(
            "URL pattern must identify one unambiguous website host: "
            f"{url_pattern}"
        )
    name = host if port is None else f"{host}%3A{port}"
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(
            f"URL pattern produced an unsafe collection name: {name}"
        )
    return name


def default_archives_root() -> Path:
    """Return the default archives root sibling of this project."""

    project_root = Path(__file__).resolve().parents[2]
    return (project_root / DEFAULT_OUTPUT_ROOT).resolve()


def collection_layout(
    url_pattern: str,
    archives_root: Path | str | None = None,
) -> CollectionLayout:
    """Build collection layout for one URL pattern."""

    root = (
        Path(archives_root).expanduser().resolve()
        if archives_root is not None
        else default_archives_root()
    )
    return CollectionLayout(root, normalize_collection_id(url_pattern))


def ensure_collection_dirs(layout: CollectionLayout) -> None:
    """Create permanent and work directories for a collection."""

    layout.root.mkdir(parents=True, exist_ok=True)
    layout.archive_root.mkdir(parents=True, exist_ok=True)
    layout.years_index_root.mkdir(parents=True, exist_ok=True)
    layout.sources_root.mkdir(parents=True, exist_ok=True)
    layout.work_root.mkdir(parents=True, exist_ok=True)


def cleanup_temps(layout: CollectionLayout) -> None:
    """Remove abandoned temporary files under the collection root."""

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
    if layout.work_root.is_dir():
        for child in layout.work_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except OSError:
                pass


def file_sha256(path: Path) -> str:
    """Return the hex SHA-256 of a file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def publish_file_atomically(source: Path, destination: Path) -> None:
    """Atomically replace destination with a complete source file."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.parent.resolve() != destination.parent.resolve():
        # Same-filesystem publish via temporary sibling of destination.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp-publish-",
            suffix=destination.suffix + ".tmp",
            dir=destination.parent,
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            shutil.copyfile(source, tmp_path)
            os.replace(tmp_path, destination)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        source.unlink(missing_ok=True)
        return
    os.replace(source, destination)


def exclusive_temp_path(directory: Path, *, suffix: str) -> Path:
    """Return an exclusive temporary path in directory."""

    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=suffix, dir=directory)
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


def list_year_warcs(layout: CollectionLayout, year: int) -> list[Path]:
    """Return finalized WARC paths for one year, sorted by sequence."""

    year_dir = layout.year_dir(year)
    if not year_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in year_dir.iterdir():
        if not path.is_file():
            continue
        match = _WARC_NAME.fullmatch(path.name)
        if match is None:
            continue
        if match.group("id") != layout.collection_id:
            continue
        if int(match.group("year")) != year:
            continue
        found.append((int(match.group("seq")), path))
    found.sort(key=lambda item: item[0])
    return [path for _, path in found]


def list_all_warcs(layout: CollectionLayout) -> list[Path]:
    """Return every finalized WARC in the collection, sorted by key."""

    if not layout.archive_root.is_dir():
        return []
    warcs = [
        path
        for path in layout.archive_root.rglob("*.warc.gz")
        if path.is_file() and not path.name.startswith(".tmp-")
    ]
    return sorted(
        warcs,
        key=lambda p: p.relative_to(layout.root).as_posix(),
    )


def next_warc_sequence(layout: CollectionLayout, year: int) -> int:
    """Return the next WARC sequence number (1-based) for a year."""

    existing = list_year_warcs(layout, year)
    if not existing:
        return 1
    last = existing[-1].name
    match = _WARC_NAME.fullmatch(last)
    assert match is not None
    nxt = int(match.group("seq")) + 1
    if nxt > 999:
        raise RuntimeError(
            f"WARC sequence would exceed 999 for {layout.collection_id} "
            f"year {year}; refusing to create shard 1000"
        )
    return nxt


def warc_artifact_from_path(
    layout: CollectionLayout,
    path: Path,
    *,
    record_count: int,
) -> WarcArtifact:
    """Build a WarcArtifact descriptor for a finalized WARC."""

    relative = path.relative_to(layout.root).as_posix()
    match = _WARC_NAME.fullmatch(path.name)
    if match is None:
        raise ValueError(f"unexpected WARC filename: {path.name}")
    return WarcArtifact(
        relative_key=relative,
        year=int(match.group("year")),
        sequence=int(match.group("seq")),
        path=path,
        size_bytes=path.stat().st_size,
        sha256=file_sha256(path),
        record_count=record_count,
    )


def index_artifact_from_path(
    layout: CollectionLayout,
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


def write_failures(
    layout: CollectionLayout,
    failures: Sequence[UnresolvedFailure],
) -> Optional[Path]:
    """Publish or remove the failures ledger."""

    if not failures:
        layout.failures_path.unlink(missing_ok=True)
        return None

    ordered = sorted(
        failures,
        key=lambda item: (
            item.identity.sort_key(),
            item.category.value,
            item.message,
        ),
    )
    payload = {
        "schema_version": FAILURES_SCHEMA_VERSION,
        "failures": [
            {
                "identity": identity_to_dict(item.identity),
                "category": item.category.value,
                "message": item.message,
            }
            for item in ordered
        ],
    }
    tmp = exclusive_temp_path(layout.root, suffix=".failures.json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    publish_file_atomically(tmp, layout.failures_path)
    return layout.failures_path


def load_failures(layout: CollectionLayout) -> list[UnresolvedFailure]:
    """Load the current failure ledger if present."""

    if not layout.failures_path.is_file():
        return []
    data = json.loads(layout.failures_path.read_text(encoding="utf-8"))
    result: list[UnresolvedFailure] = []
    for row in data.get("failures", []):
        result.append(
            UnresolvedFailure(
                identity=identity_from_dict(row["identity"]),
                category=FailureCategory(row["category"]),
                message=str(row["message"]),
            )
        )
    return result


def write_manifest(
    layout: CollectionLayout,
    *,
    url_pattern: str,
    status: str,
    run_source_relative: Optional[str],
    warcs: Sequence[WarcArtifact],
    annual_indexes: Sequence[IndexArtifact],
    collection_index: Optional[IndexArtifact],
    metrics: RunMetrics,
) -> Path:
    """Atomically publish collection.json."""

    if status not in {"complete", "partial"}:
        raise ValueError(f"invalid collection status: {status}")
    payload = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collection_id": layout.collection_id,
        "url_pattern": url_pattern,
        "warc_version": WARC_VERSION,
        "warc_target_bytes": WARC_TARGET_BYTES,
        "status": status,
        "run_source": run_source_relative,
        "counts": {
            "selected": metrics.selected,
            "represented": metrics.represented,
            "locally_reused": metrics.local_reuses,
            "downloaded": metrics.downloads,
            "revisited": metrics.revisits,
            "unresolved": metrics.unresolved,
        },
        "metrics": {
            "cdx_requests": metrics.cdx_requests,
            "cdx_duration_s": round(metrics.cdx_duration_s, 3),
            "playback_starts": metrics.playback_starts,
            "playback_completions": metrics.playback_completions,
            "playback_bytes": metrics.playback_bytes,
            "peak_connections": metrics.peak_connections,
            "rate_gate_wait_s": round(metrics.rate_gate_wait_s, 3),
            "cooldown_wait_s": round(metrics.cooldown_wait_s, 3),
            "warc_write_s": round(metrics.warc_write_s, 3),
            "index_s": round(metrics.index_s, 3),
            "attempts_by_category": dict(
                sorted(metrics.attempts_by_category.items())
            ),
        },
        "warcs": [
            {
                "filename": item.relative_key,
                "year": item.year,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "record_count": item.record_count,
            }
            for item in sorted(warcs, key=lambda w: w.relative_key)
        ],
        "annual_indexes": [
            {
                "filename": item.relative_key,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "capture_count": item.capture_count,
            }
            for item in sorted(annual_indexes, key=lambda i: i.relative_key)
        ],
        "collection_index": (
            {
                "filename": collection_index.relative_key,
                "size_bytes": collection_index.size_bytes,
                "sha256": collection_index.sha256,
                "capture_count": collection_index.capture_count,
            }
            if collection_index is not None
            else None
        ),
    }
    tmp = exclusive_temp_path(layout.root, suffix=".collection.json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    publish_file_atomically(tmp, layout.manifest_path)
    return layout.manifest_path


