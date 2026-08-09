"""Resolve domain archives and their flat portable collections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_ARCHIVE_IDS = frozenset({"static"})


@dataclass(frozen=True)
class ReplayCollection:
    """One independently portable WARC/CDXJ collection."""

    collection_id: str
    root: Path
    replay_index: Path


@dataclass(frozen=True)
class Archive:
    """One domain route aggregating portable replay collections."""

    archive_id: str
    root: Path
    collections: tuple[ReplayCollection, ...]


def resolve_archives_root(value: Path | str) -> Path:
    """Return one readable, absolute archives directory."""

    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            f"archives root does not exist or cannot be resolved: {path}"
        ) from error
    if not resolved.is_dir():
        raise ValidationError(f"archives root is not a directory: {resolved}")
    return resolved


def validate_archive_id(archive_id: str) -> str:
    if (
        not _SAFE_ID.fullmatch(archive_id)
        or archive_id in {".", ".."}
        or archive_id in _RESERVED_ARCHIVE_IDS
    ):
        raise ValidationError(f"invalid archive ID: {archive_id!r}")
    return archive_id


def validate_collection_id(collection_id: str) -> str:
    if not _SAFE_ID.fullmatch(collection_id) or collection_id in {".", ".."}:
        raise ValidationError(f"invalid collection ID: {collection_id!r}")
    return collection_id


def select_archive(archives_root: Path, archive_id: str) -> Archive:
    """Resolve one contained domain archive by ID."""

    archive_id = validate_archive_id(archive_id)
    return _validate_archive(archives_root, archive_id, archives_root / archive_id)


def discover_archives(archives_root: Path) -> tuple[Archive, ...]:
    """Discover every immediate domain archive beneath the archives root."""

    try:
        entries = sorted(
            (entry for entry in archives_root.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
    except OSError as error:
        raise ValidationError(
            f"cannot list archives root: {archives_root}: {error}"
        ) from error
    if not entries:
        raise ValidationError(f"no domain archives found beneath: {archives_root}")

    archives: list[Archive] = []
    failures: list[str] = []
    for candidate in entries:
        try:
            archives.append(_validate_archive(archives_root, candidate.name, candidate))
        except ValidationError as error:
            failures.append(str(error))
    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise ValidationError(f"invalid archives beneath {archives_root}:\n{details}")
    return tuple(archives)


def _validate_archive(archives_root: Path, archive_id: str, candidate: Path) -> Archive:
    validate_archive_id(archive_id)
    root = _resolve_immediate_child(archives_root, candidate, f"archive {archive_id!r}")
    collections_root = root / "collections"
    try:
        resolved_collections = collections_root.resolve(strict=True)
        resolved_collections.relative_to(root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValidationError(
            f"archive {archive_id!r} has no safe collections directory: "
            f"{collections_root}"
        ) from error
    if not resolved_collections.is_dir():
        raise ValidationError(
            f"archive {archive_id!r} collections path is not a directory: "
            f"{resolved_collections}"
        )

    entries = sorted(
        (entry for entry in resolved_collections.iterdir() if entry.is_dir()),
        key=lambda entry: entry.name,
    )
    if not entries:
        raise ValidationError(f"archive {archive_id!r} has no playable collections")
    collections = tuple(
        _validate_replay_collection(root, archive_id, resolved_collections, entry)
        for entry in entries
    )
    return Archive(archive_id=archive_id, root=root, collections=collections)


def _validate_replay_collection(
    archive_root: Path,
    archive_id: str,
    collections_root: Path,
    candidate: Path,
) -> ReplayCollection:
    collection_id = validate_collection_id(candidate.name)
    root = _resolve_immediate_child(
        collections_root,
        candidate,
        f"collection {collection_id!r} in archive {archive_id!r}",
    )
    replay_index = root / f"{archive_id}-{collection_id}-index.cdxj"
    return ReplayCollection(collection_id, root, replay_index)


def _resolve_immediate_child(parent: Path, candidate: Path, label: str) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(f"{label} cannot be resolved: {candidate}") from error
    if resolved.parent != parent or not resolved.is_dir():
        raise ValidationError(f"{label} is not a contained directory: {candidate}")
    return resolved
