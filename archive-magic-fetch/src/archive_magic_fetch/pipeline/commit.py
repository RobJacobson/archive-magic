"""Prepare, promote, abort, and recover annual archive transactions."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Sequence

from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs
from archive_magic_fetch.models import WarcArtifact
from archive_magic_fetch.runtime.filesystem import file_sha256


class YearStage:
    """Keep one year's mutable tail and index separate from canonical data."""

    @staticmethod
    def recover(layout: ArchiveLayout) -> None:
        """Finish validated promotions; discard interrupted, unvalidated work."""

        staging_root = layout.root / ".staging"
        if not staging_root.is_dir():
            return
        for stage in sorted(staging_root.iterdir()):
            if not stage.is_dir():
                raise ValueError(f"unexpected staging entry: {stage}")
            ready = stage / "ready.json"
            if ready.is_file():
                manifest = json.loads(ready.read_text(encoding="utf-8"))
                if manifest["year"] != stage.name:
                    raise ValueError(f"staging year mismatch: {stage}")
                _promote(layout, stage, manifest)
            else:
                shutil.rmtree(stage)
        staging_root.rmdir()

    def __init__(self, canonical: ArchiveLayout, year: str, *, reset: bool = False):
        canonical.validate_collection_id(year)
        self.canonical = canonical
        self.year = year
        self.reset = reset
        self.path = canonical.root / ".staging" / year
        if self.path.exists():
            raise ValueError(f"unrecovered year staging directory: {self.path}")
        self.path.mkdir(parents=True)
        self.layout = ArchiveLayout(self.path, canonical.archive_id)
        if reset:
            return
        warcs = list_collection_warcs(canonical, year)
        for path in warcs:
            target = self.path / path.name
            try:
                os.link(path, target)
            except OSError:
                shutil.copy2(path, target)
        index = canonical.collection_index(year)
        if index.is_file():
            shutil.copy2(index, self.layout.collection_index(year))

    def prepare_mutable_tail(self, target_bytes: int) -> None:
        """Break the final shard's hard link only when new captures need writing."""

        if self.reset:
            return
        warcs = list_collection_warcs(self.canonical, self.year)
        if not warcs:
            return
        source = warcs[-1]
        if source.stat().st_size >= target_bytes:
            return
        destination = self.path / source.name
        temporary = self.path / f".tmp-tail-{source.name}"
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def abort(self) -> None:
        if self.path.exists() and not (self.path / "ready.json").exists():
            shutil.rmtree(self.path)
        parent = self.path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    def commit(
        self,
        changed_warcs: Sequence[WarcArtifact],
        *,
        index_changed: bool,
    ) -> None:
        """Record a recoverable transaction, then install WARC bytes before index."""

        if self.reset:
            warc_paths = list_collection_warcs(self.layout, self.year)
        else:
            warc_paths = [item.path for item in changed_warcs]
        index = self.layout.collection_index(self.year)
        index_entry = (
            _description(index)
            if index.is_file() and (index_changed or self.reset)
            else None
        )
        if not warc_paths and index_entry is None and not self.reset:
            self.abort()
            return
        staged_names = {path.name for path in warc_paths}
        deleted = (
            [
                path.name
                for path in list_collection_warcs(self.canonical, self.year)
                if path.name not in staged_names
            ]
            if self.reset
            else []
        )
        manifest = {
            "year": self.year,
            "reset": self.reset,
            "warcs": [_description(path) for path in warc_paths],
            "index": index_entry,
            "delete": deleted,
        }
        temporary = self.path / "ready.json.tmp"
        temporary.write_text(json.dumps(manifest), encoding="utf-8")
        os.replace(temporary, self.path / "ready.json")
        _promote(self.canonical, self.path, manifest)
        if self.path.parent.is_dir() and not any(self.path.parent.iterdir()):
            self.path.parent.rmdir()


def _description(path: Path) -> dict[str, object]:
    return {"name": path.name, "size": path.stat().st_size, "sha256": file_sha256(path)}


def _matches(path: Path, entry: dict[str, object]) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == entry["size"]
        and file_sha256(path) == entry["sha256"]
    )


def _promote(
    canonical: ArchiveLayout, stage: Path, manifest: dict[str, object]
) -> None:
    year = canonical.validate_collection_id(str(manifest["year"]))
    index = manifest["index"]
    if manifest["reset"] and (index is None or (stage / str(index["name"])).is_file()):
        canonical.collection_index(year).unlink(missing_ok=True)
    for entry in manifest["warcs"]:
        name = str(entry["name"])
        source = stage / name
        target = canonical.root / name
        if source.is_file():
            if not _matches(source, entry):
                raise ValueError(f"staged WARC changed during promotion: {name}")
            os.replace(source, target)
        elif not _matches(target, entry):
            raise ValueError(f"cannot recover promoted WARC: {name}")
    if index is not None:
        name = str(index["name"])
        source = stage / name
        target = canonical.root / name
        if source.is_file():
            if not _matches(source, index):
                raise ValueError(f"staged index changed during promotion: {name}")
            os.replace(source, target)
        elif not _matches(target, index):
            raise ValueError(f"cannot recover promoted index: {name}")
    for name in manifest["delete"]:
        (canonical.root / str(name)).unlink(missing_ok=True)
    shutil.rmtree(stage)
