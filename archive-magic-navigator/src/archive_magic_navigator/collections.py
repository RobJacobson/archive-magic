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
    """One yearly WARC/CDXJ partition in an archive."""

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
    """Validate an exact archive data root from a configuration."""

    archive_id = validate_archive_id(archive_id)
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(f"archive data does not exist: {root}") from error
    if not resolved.is_dir():
        raise ValidationError(f"archive data is not a directory: {resolved}")
    return _validate_archive(archive_id, resolved)


def _validate_archive(archive_id: str, root: Path) -> Archive:
    validate_archive_id(archive_id)
    pattern = f"{archive_id}-*-index.cdxj"
    collections = tuple(
        _validate_replay_collection(root, archive_id, index)
        for index in sorted(root.glob(pattern))
        if index.is_file()
    )
    if not collections:
        raise ValidationError(f"archive {archive_id!r} has no playable collections")
    return Archive(archive_id=archive_id, root=root, collections=collections)


def _validate_replay_collection(
    root: Path,
    archive_id: str,
    replay_index: Path,
) -> ReplayCollection:
    prefix, suffix = f"{archive_id}-", "-index.cdxj"
    collection_id = validate_collection_id(
        replay_index.name[len(prefix) : -len(suffix)]
    )
    try:
        resolved = replay_index.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            f"collection index cannot be resolved: {replay_index}"
        ) from error
    if resolved.parent != root:
        raise ValidationError(f"collection index is not contained: {replay_index}")
    return ReplayCollection(collection_id, root, resolved)
