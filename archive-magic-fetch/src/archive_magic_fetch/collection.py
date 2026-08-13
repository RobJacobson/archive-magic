"""Domain archive layout, atomic publication, and immutable run records."""

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
    DEFAULT_OUTPUT_ROOT,
    RUN_SCHEMA_VERSION,
    WARC_TARGET_BYTES,
    WARC_VERSION,
    IndexArtifact,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
    identity_to_dict,
)


_WWW_ALIAS_PREFIX = re.compile(r"^www\d*\.")
_TEMP_NAME = re.compile(r"^\.tmp-|^.*\.tmp$")
_PARTIAL_WARC_SUFFIX = ".warc.gz.partial"
_COLLECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LEGACY_NAMES = ("archive", "sources", "index.cdxj", "collection.json", "failures.json")


@dataclass(frozen=True)
class ArchiveLayout:
    """Filesystem boundaries for one domain archive and its collections."""

    archives_root: Path
    archive_id: str

    @property
    def root(self) -> Path:
        return self.archives_root / self.archive_id

    @property
    def collections_root(self) -> Path:
        return self.root / "collections"

    @property
    def captures_root(self) -> Path:
        return self.root / "captures"

    @property
    def work_root(self) -> Path:
        return self.captures_root / ".work"

    def validate_collection_id(self, collection_id: str) -> str:
        if not _COLLECTION_ID.fullmatch(collection_id) or collection_id in {".", ".."}:
            raise ValueError(f"unsafe collection ID: {collection_id!r}")
        return collection_id

    def validate_run_id(self, run_id: str) -> str:
        if not _COLLECTION_ID.fullmatch(run_id) or run_id in {".", ".."}:
            raise ValueError(f"unsafe run ID: {run_id!r}")
        return run_id

    def collection_dir(self, collection_id: str) -> Path:
        return self.collections_root / self.validate_collection_id(collection_id)

    def capture_dir(self, collection_id: str) -> Path:
        return self.captures_root / self.validate_collection_id(collection_id)

    def run_dir(self, collection_id: str, run_id: str) -> Path:
        return self.capture_dir(collection_id) / "runs" / self.validate_run_id(run_id)

    def run_record(self, collection_id: str, run_id: str) -> Path:
        return self.run_dir(collection_id, run_id) / "run.json"

    def index_filename(self, collection_id: str) -> str:
        collection_id = self.validate_collection_id(collection_id)
        return f"{self.archive_id}-{collection_id}-index.cdxj"

    def collection_index(self, collection_id: str) -> Path:
        return self.collection_dir(collection_id) / self.index_filename(collection_id)

    def collection_warc_filename(self, collection_id: str, sequence: int) -> str:
        collection_id = self.validate_collection_id(collection_id)
        if sequence < 1 or sequence > 999:
            raise ValueError(
                f"WARC sequence must be 001-999, got {sequence}"
            )
        return f"{self.archive_id}-{collection_id}-{sequence:03d}.warc.gz"

    def collection_warc_path(self, collection_id: str, sequence: int) -> Path:
        return self.collection_dir(collection_id) / self.collection_warc_filename(
            collection_id, sequence
        )

    def collection_warc_partial_path(
        self, collection_id: str, sequence: int
    ) -> Path:
        """Return the visible in-progress sibling of one WARC shard."""

        final = self.collection_warc_path(collection_id, sequence)
        return final.with_name(final.name + ".partial")


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


def normalize_archive_id(url_pattern: str) -> str:
    """Derive one safe domain-archive directory name from a URL pattern."""

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
            f"URL pattern produced an unsafe archive name: {name}"
        )
    return name


def default_archives_root() -> Path:
    """Return the default archives root sibling of this project."""

    project_root = Path(__file__).resolve().parents[2]
    return (project_root / DEFAULT_OUTPUT_ROOT).resolve()


def archive_layout(
    url_pattern: str,
    archives_root: Path | str | None = None,
) -> ArchiveLayout:
    """Build domain-archive layout for one URL pattern."""

    root = (
        Path(archives_root).expanduser().resolve()
        if archives_root is not None
        else default_archives_root()
    )
    return ArchiveLayout(root, normalize_archive_id(url_pattern))


def reject_legacy_layout(layout: ArchiveLayout) -> None:
    """Reject pre-flat Archive Magic output rather than mixing schemas."""

    found = [name for name in _LEGACY_NAMES if (layout.root / name).exists()]
    if found:
        raise ValueError(
            "unsupported legacy archive layout; delete and regenerate the archive "
            f"(found: {', '.join(found)})"
        )


def ensure_collection_dirs(layout: ArchiveLayout) -> None:
    """Create permanent domain archive and capture-state directories."""

    layout.root.mkdir(parents=True, exist_ok=True)
    layout.collections_root.mkdir(parents=True, exist_ok=True)
    layout.captures_root.mkdir(parents=True, exist_ok=True)


def cleanup_temps(layout: ArchiveLayout) -> None:
    """Remove abandoned short-lived temps; keep visible WARC partials."""

    if not layout.root.is_dir():
        return
    for path in layout.root.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(_PARTIAL_WARC_SUFFIX):
            continue
        if _TEMP_NAME.match(path.name):
            try:
                path.unlink()
            except OSError:
                pass
    if layout.work_root.is_dir():
        try:
            next(layout.work_root.iterdir())
        except StopIteration:
            shutil.rmtree(layout.work_root, ignore_errors=True)
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


def _collection_warc_name_pattern(
    layout: ArchiveLayout, collection_id: str
) -> re.Pattern[str]:
    """Return the basename pattern for one portable collection's WARC shards."""

    return re.compile(
        rf"{re.escape(layout.archive_id)}-{re.escape(collection_id)}-"
        r"(?P<seq>\d{3})\.warc\.gz"
    )


def reset_collection_data(layout: ArchiveLayout, collection_id: str) -> None:
    """Remove WARC, partial, and CDXJ artifacts for one portable collection."""

    collection_id = layout.validate_collection_id(collection_id)
    for path in list_collection_warcs(layout, collection_id):
        path.unlink()
    for path in list_collection_partials(layout, collection_id):
        path.unlink()
    index_path = layout.collection_index(collection_id)
    if index_path.is_file():
        index_path.unlink()


def list_collection_warcs(
    layout: ArchiveLayout, collection_id: str
) -> list[Path]:
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


def list_collection_partials(
    layout: ArchiveLayout, collection_id: str
) -> list[Path]:
    """Return visible in-progress WARC partials for one collection."""

    collection_id = layout.validate_collection_id(collection_id)
    collection_dir = layout.collection_dir(collection_id)
    if not collection_dir.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for path in collection_dir.iterdir():
        if not path.is_file():
            continue
        parsed = parse_warc_partial_name(layout, collection_id, path.name)
        if parsed is None:
            continue
        found.append((parsed, path))
    found.sort(key=lambda item: item[0])
    return [path for _, path in found]


def parse_warc_partial_name(
    layout: ArchiveLayout, collection_id: str, name: str
) -> int | None:
    """Return the shard sequence encoded in a WARC partial basename."""

    collection_id = layout.validate_collection_id(collection_id)
    pattern = re.compile(
        rf"^(?:\.tmp-[^.]+\.)?{re.escape(layout.archive_id)}-"
        rf"{re.escape(collection_id)}-(?P<seq>\d{{3}})\.warc\.gz\.partial$"
    )
    match = pattern.fullmatch(name)
    if match is None:
        return None
    return int(match.group("seq"))


def last_collection_warc(
    layout: ArchiveLayout, collection_id: str
) -> tuple[int, Path] | None:
    """Return the highest-sequence finalized WARC, if any."""

    existing = list_collection_warcs(layout, collection_id)
    if not existing:
        return None
    last = existing[-1]
    match = _collection_warc_name_pattern(layout, collection_id).fullmatch(
        last.name
    )
    assert match is not None
    return int(match.group("seq")), last


def next_collection_warc_sequence(
    layout: ArchiveLayout, collection_id: str
) -> int:
    """Return the next WARC sequence number for a portable collection."""

    collection_id = layout.validate_collection_id(collection_id)
    existing = list_collection_warcs(layout, collection_id)
    if not existing:
        return 1
    last = existing[-1].name
    match = _collection_warc_name_pattern(layout, collection_id).fullmatch(last)
    assert match is not None
    nxt = int(match.group("seq")) + 1
    if nxt > 999:
        raise RuntimeError(
            f"WARC sequence would exceed 999 for {layout.archive_id} "
            f"collection {collection_id}; refusing to create shard 1000"
        )
    return nxt


def warc_artifact_from_path(
    layout: ArchiveLayout,
    path: Path,
    *,
    record_count: int,
) -> WarcArtifact:
    """Build a WarcArtifact descriptor for a finalized WARC."""

    relative = path.relative_to(layout.root).as_posix()
    collection_id = path.parent.name
    layout.validate_collection_id(collection_id)
    expected_parent = layout.collection_dir(collection_id)
    match = _collection_warc_name_pattern(layout, collection_id).fullmatch(
        path.name
    )
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


def write_run_record(
    layout: ArchiveLayout,
    *,
    collection_id: str,
    run_id: str,
    url_pattern: str,
    date_start: str,
    date_end: str,
    query: dict[str, object],
    warcs: Sequence[WarcArtifact],
    index: Optional[IndexArtifact],
    metrics: RunMetrics,
    failures: Sequence[UnresolvedFailure],
) -> Path:
    """Atomically publish the immutable completion record for one run slice."""

    ordered_failures = sorted(
        failures,
        key=lambda item: (item.identity.sort_key(), item.category.value, item.message),
    )
    payload = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "archive_id": layout.archive_id,
        "collection_id": collection_id,
        "url_pattern": url_pattern,
        "date_start": date_start,
        "date_end": date_end,
        "warc_version": WARC_VERSION,
        "warc_target_bytes": WARC_TARGET_BYTES,
        "query": query,
        "counts": {
            "selected": metrics.selected,
            "represented": metrics.represented,
            "locally_reused": metrics.local_reuses,
            "payload_reused": metrics.payload_reuses,
            "downloaded": metrics.downloads,
            "revisited": metrics.revisits,
            "digest_mismatch_accepted": metrics.digest_mismatch_accepted,
            "unresolved": metrics.unresolved,
        },
        "metrics": {
            "cdx_requests": metrics.cdx_requests,
            "cdx_duration_s": round(metrics.cdx_duration_s, 3),
            "playback_attempts": metrics.playback_attempts,
            "playback_bytes": metrics.playback_bytes,
            "warc_write_s": round(metrics.warc_write_s, 3),
            "index_s": round(metrics.index_s, 3),
            "attempts_by_category": dict(
                sorted(metrics.attempts_by_category.items())
            ),
        },
        "warcs": [
            {
                "filename": item.relative_key,
                "collection_id": item.collection_id,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "record_count": item.record_count,
            }
            for item in sorted(warcs, key=lambda w: w.relative_key)
        ],
        "index": (
            {
                "filename": index.relative_key,
                "size_bytes": index.size_bytes,
                "sha256": index.sha256,
                "capture_count": index.capture_count,
            }
            if index is not None
            else None
        ),
        "failures": [
            {
                "identity": identity_to_dict(item.identity),
                "category": item.category.value,
                "message": item.message,
            }
            for item in ordered_failures
        ],
    }
    destination = layout.run_record(collection_id, run_id)
    if destination.exists():
        raise FileExistsError(f"run record already exists: {destination}")
    tmp = exclusive_temp_path(destination.parent, suffix=".run.json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    publish_file_atomically(tmp, destination)
    return destination
