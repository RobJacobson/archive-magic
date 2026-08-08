"""Resolve and discover Archive Magic collections."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError


_COLLECTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RESERVED_COLLECTION_IDS = frozenset({"static"})


@dataclass(frozen=True)
class Collection:
    """One collection selected beneath an archives root."""

    collection_id: str
    root: Path

    @property
    def replay_index(self) -> Path:
        return self.root / "index.cdxj"


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


def validate_collection_id(collection_id: str) -> str:
    """Require one immediate directory name, never an arbitrary path."""

    if (
        not _COLLECTION_ID.fullmatch(collection_id)
        or collection_id in {".", ".."}
        or collection_id in _RESERVED_COLLECTION_IDS
    ):
        raise ValidationError(f"invalid collection ID: {collection_id!r}")
    return collection_id


def select_collection(
    archives_root: Path,
    collection_id: str,
) -> Collection:
    """Resolve one contained collection by ID."""

    collection_id = validate_collection_id(collection_id)
    candidate = archives_root / collection_id
    return _validate_candidate(archives_root, collection_id, candidate)


def discover_collections(archives_root: Path) -> tuple[Collection, ...]:
    """Discover and validate every immediate collection directory."""

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
        raise ValidationError(
            f"no collection directories found beneath: {archives_root}"
        )

    collections: list[Collection] = []
    failures: list[str] = []
    for candidate in entries:
        try:
            collections.append(
                _validate_candidate(
                    archives_root,
                    candidate.name,
                    candidate,
                )
            )
        except ValidationError as error:
            failures.append(str(error))
    if failures:
        details = "\n".join(f"  - {failure}" for failure in failures)
        raise ValidationError(
            f"invalid collections beneath {archives_root}:\n{details}"
        )
    return tuple(collections)


def _validate_candidate(
    archives_root: Path,
    collection_id: str,
    candidate: Path,
) -> Collection:
    validate_collection_id(collection_id)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValidationError(
            f"collection {collection_id!r} cannot be resolved: {candidate}"
        ) from error

    if resolved.parent != archives_root:
        raise ValidationError(
            f"collection {collection_id!r} escapes archives root: {candidate}"
        )
    if not resolved.is_dir():
        raise ValidationError(
            f"collection {collection_id!r} is not a directory: {resolved}"
        )
    return Collection(collection_id=collection_id, root=resolved)
