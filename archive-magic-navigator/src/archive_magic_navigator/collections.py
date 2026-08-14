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
    archive_path: str | None = None


@dataclass(frozen=True)
class Archive:
    """One domain route aggregating portable replay collections."""

    archive_id: str
    root: Path
    collections: tuple[ReplayCollection, ...]


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


def select_archive_root(root: Path, archive_id: str) -> Archive:
    """Validate an exact archive workspace root from a descriptor."""

    archive_id = validate_archive_id(archive_id)
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(f"archive workspace does not exist: {root}") from error
    if not resolved.is_dir():
        raise ValidationError(f"archive workspace is not a directory: {resolved}")
    return _validate_archive(archive_id, resolved)


def _validate_archive(archive_id: str, root: Path) -> Archive:
    validate_archive_id(archive_id)
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
    collections = tuple(
        collection
        for collection in (
            _validate_replay_collection(
                root, archive_id, resolved_collections, entry
            )
            for entry in entries
        )
        if collection is not None
    )
    if not collections:
        raise ValidationError(f"archive {archive_id!r} has no playable collections")
    return Archive(archive_id=archive_id, root=root, collections=collections)


def _validate_replay_collection(
    archive_root: Path,
    archive_id: str,
    collections_root: Path,
    candidate: Path,
) -> ReplayCollection | None:
    collection_id = validate_collection_id(candidate.name)
    root = _resolve_immediate_child(
        collections_root,
        candidate,
        f"collection {collection_id!r} in archive {archive_id!r}",
    )
    replay_index = root / f"{archive_id}-{collection_id}-index.cdxj"
    if not replay_index.is_file():
        return None
    return ReplayCollection(collection_id, root, replay_index)


def _resolve_immediate_child(parent: Path, candidate: Path, label: str) -> Path:
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(f"{label} cannot be resolved: {candidate}") from error
    if resolved.parent != parent or not resolved.is_dir():
        raise ValidationError(f"{label} is not a contained directory: {candidate}")
    return resolved
