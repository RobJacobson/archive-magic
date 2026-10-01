"""Validated index snapshots for local archives; WARCs stay at the source."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from .collections import Archive, select_archive_root
from .errors import ValidationError
from .store import IndexStore, publish_indexes
from .validation import validate_collection


class LocalArchiveStore(IndexStore):
    def __init__(
        self, directory: Path, cache_directory: Path, poll_seconds: float
    ) -> None:
        super().__init__(cache_directory, poll_seconds)
        self.directory = directory
        self._states: dict[str, Archive] = {}
        self._signatures: dict[Path, tuple[int, ...]] = {}

    def load_archive(self, archive_id: str) -> Archive:
        self._poll_archive(archive_id)
        return self._states[archive_id]

    def _poll_archive(self, archive_id: str) -> None:
        discovered = select_archive_root(self.directory, archive_id)
        previous = self._states.get(archive_id)
        accepted = (
            {item.collection_id: item for item in previous.collections}
            if previous else {}
        )
        cache = (self.cache_directory / archive_id / "local").resolve()
        cache.mkdir(parents=True, exist_ok=True)
        signatures = self._signatures.copy()
        staged: list[tuple[Path, Path]] = []
        try:
            for collection in discovered.collections:
                source = collection.replay_index
                signature = _signature(source)
                if signatures.get(source) == signature:
                    continue
                with tempfile.NamedTemporaryFile(
                    dir=cache, prefix=".tmp-index-", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                target = cache / source.name
                staged.append((target, temporary))
                shutil.copyfile(source, temporary)
                validate_collection(
                    collection, archive_id=archive_id, staged_index=temporary
                )
                if _signature(source) != signature:
                    raise ValidationError(f"index changed while being copied: {source}")
                signatures[source] = signature
                accepted[collection.collection_id] = replace(
                    collection, replay_index=target
                )
            if not staged:
                return
            archive = replace(
                discovered,
                collections=tuple(accepted[key] for key in sorted(accepted)),
                replay_index=cache / ".replay.cdxj",
            )
            publish_indexes(archive, staged)
            self._states[archive_id] = archive
            self._signatures = signatures
        finally:
            for _, temporary in staged:
                temporary.unlink(missing_ok=True)


def _signature(path: Path) -> tuple[int, ...]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
