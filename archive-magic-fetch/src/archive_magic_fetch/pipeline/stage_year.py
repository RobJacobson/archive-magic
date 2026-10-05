"""Prepare, promote, abort, and recover annual archive transactions."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs, warc_artifact_from_path
from archive_magic_fetch.archive.inventory_collection import CollectionInventory, inventory_collection
from archive_magic_fetch.archive.scan_warcs import WarcReadOptions, WarcScan
from archive_magic_fetch.pipeline.build_collection_index import prepare_collection_index
from archive_magic_fetch.models import IndexArtifact, WarcArtifact
from archive_magic_fetch.runtime.manage_archive_files import (
    file_sha256, mkdir_durably, publish_file_atomically, sync_file,
    sync_directory, write_json_durably, exclusive_temp_path,
)
from archive_magic_fetch.runtime.report_progress import emit


@dataclass
class YearWorkState:
    version: int
    binding: dict[str, object]
    baseline: dict[str, dict[str, object]]
    reset: bool
    durable_sizes: dict[str, int]
    initialized: bool = False


@dataclass(frozen=True)
class YearChanges:
    """One verified generation shared by promotion, publication, and run logging."""

    warcs: tuple[WarcArtifact, ...]
    index: IndexArtifact | None
    changed_warcs: tuple[WarcArtifact, ...]
    index_changed: bool
    deleted: tuple[str, ...]

    @property
    def artifacts(self) -> tuple[WarcArtifact | IndexArtifact, ...]:
        index = (self.index,) if self.index_changed and self.index is not None else ()
        return (*self.changed_warcs, *index)


class YearStage:
    """Keep one year's mutable tail and index separate from canonical data."""

    @staticmethod
    def recover(layout: ArchiveLayout) -> None:
        """Finish validated promotions while preserving unfinished acquisitions."""

        staging_root = layout.root / ".staging"
        if staging_root.is_symlink():
            raise ValueError(f"staging must not be a symlink: {staging_root}")
        if not staging_root.is_dir():
            return
        for stage in sorted(staging_root.iterdir()):
            if stage.is_symlink() or not stage.is_dir():
                raise ValueError(f"unexpected staging entry: {stage}")
            ready = stage / "ready.json"
            if ready.is_file():
                manifest = json.loads(ready.read_text(encoding="utf-8"))
                if manifest["year"] != stage.name:
                    raise ValueError(f"staging year mismatch: {stage}")
                _promote(layout, stage, manifest)
        if not any(staging_root.iterdir()):
            staging_root.rmdir()
            sync_directory(layout.root)

    def __init__(
        self, canonical: ArchiveLayout, year: str, *, reset: bool = False,
        binding: dict[str, object] | None = None,
    ):
        canonical.validate_collection_id(year)
        self.canonical = canonical
        self.year = year
        self.path = canonical.root / ".staging" / year
        self.layout = ArchiveLayout(self.path, canonical.archive_id)
        self.state_path = self.path / "work.json"
        self.scans: dict[str, WarcScan] = {}
        expected = {"archive_id": canonical.archive_id, "year": year, "format": 1,
                    **(binding or {})}
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ValueError(f"staging must not be a symlink: {self.path}")
        if reset and self.path.exists():
            if (self.path / "ready.json").exists():
                raise ValueError("recover validated promotion before resetting")
            self.discard()
        self.resumed = self.path.exists()
        if self.resumed:
            try:
                if self.state_path.is_symlink():
                    raise ValueError("work state must not be a symlink")
                document = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.state = YearWorkState(**document)
                self._validate_state()
            except (OSError, TypeError, ValueError) as error:
                raise ValueError(f"invalid or legacy unfinished stage {self.path}; "
                                 "preserved; use matching settings or --reset-data") from error
            if self.state.binding != expected:
                raise ValueError(f"unfinished stage settings mismatch: {self.path}; "
                                 "use matching settings or --reset-data")
            if self._baseline() != self.state.baseline:
                raise ValueError(f"canonical year changed since acquisition: {self.path}")
        else:
            mkdir_durably(self.path)
            self.state = YearWorkState(1, expected, self._baseline(), reset, {})
            self._save()
        self.reset = self.state.reset
        if not self.state.initialized:
            self._seed_baseline()

    def _baseline(self) -> dict[str, dict[str, object]]:
        paths = list_collection_warcs(self.canonical, self.year)
        index = self.canonical.collection_index(self.year)
        if index.is_file():
            paths.append(index)
        return {path.name: _description(path) for path in paths}

    def _validate_state(self) -> None:
        state = self.state
        if (type(state.version) is not int or state.version != 1
                or not isinstance(state.binding, dict)
                or not isinstance(state.baseline, dict)
                or not isinstance(state.durable_sizes, dict)
                or type(state.reset) is not bool or type(state.initialized) is not bool):
            raise ValueError("invalid work-state envelope")
        prefix = re.escape(f"{self.canonical.archive_id}-{self.year}-")
        warc = prefix + r"[0-9]{3,}\.warc\.gz"
        for name, entry in state.baseline.items():
            if (not re.fullmatch(prefix + r"(?:[0-9]{3,}\.warc\.gz|index\.cdxj)", name)
                    or not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}
                    or entry["name"] != name or type(entry["size"]) is not int
                    or entry["size"] < 0 or not isinstance(entry["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise ValueError("invalid baseline file signature")
        for name, size in state.durable_sizes.items():
            if not re.fullmatch(warc, name) or type(size) is not int or size < 0:
                raise ValueError("invalid durable shard offset")
        if state.initialized and not state.reset:
            for name, entry in state.baseline.items():
                if name.endswith(".warc.gz") and state.durable_sizes.get(name, -1) < entry["size"]:
                    raise ValueError("baseline WARC bytes missing from checkpoint")

    def _save(self) -> None:
        write_json_durably(self.state_path, asdict(self.state))

    def _seed_baseline(self) -> None:
        if not self.reset:
            for name, entry in self.state.baseline.items():
                source, target = self.canonical.root / name, self.path / name
                if not target.exists():
                    if name.endswith(".warc.gz"):
                        try:
                            os.link(source, target)
                        except OSError:
                            shutil.copy2(source, target)
                    else:
                        shutil.copy2(source, target)
                    sync_file(target)
                if not _matches(target, entry):
                    raise ValueError(f"incomplete baseline copy: {target}")
                if name.endswith(".warc.gz"):
                    self.state.durable_sizes[name] = int(entry["size"])
        sync_directory(self.path)
        self.state.initialized = True
        self._save()

    def prepare_mutable_tail(self, target_bytes: int) -> None:
        """Detach only a shared working tail; never overwrite resumed bytes."""
        warcs = list_collection_warcs(self.layout, self.year)
        if not warcs or warcs[-1].stat().st_size >= target_bytes:
            return
        destination = warcs[-1]
        source = self.canonical.root / destination.name
        if not source.is_file() or not os.path.samefile(source, destination):
            return
        temporary = exclusive_temp_path(self.path, suffix=".tail.tmp")
        try:
            shutil.copy2(source, temporary)
            publish_file_atomically(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare_inventory(self) -> CollectionInventory:
        """Recover and index the working view before loading CDXJ inventory."""
        paths = list_collection_warcs(self.layout, self.year)
        names = {path.name for path in paths}
        if self.state.durable_sizes.keys() - names:
            raise ValueError("checkpointed WARC shard is missing")
        recovered: list[Path] = []
        options: dict[Path, WarcReadOptions] = {}
        self.scans.clear()
        for number, path in enumerate(paths):
            if path.is_symlink():
                raise ValueError(f"working WARC must not be a symlink: {path}")
            durable = self.state.durable_sizes.get(path.name, 0)
            canonical = self.canonical.root / path.name
            private = not canonical.exists() or not os.path.samefile(path, canonical)
            repair = number == len(paths) - 1 and private
            if path.stat().st_size < durable:
                raise ValueError(f"checkpointed WARC bytes are missing: {path}")
            options[path] = WarcReadOptions(self.year, durable, repair, allow_empty=True)

        with prepare_collection_index(self.layout, self.year, read_options=options) as prepared:
            for path, scanned in prepared.scans.items():
                permission = options[path]
                if not scanned.captures:
                    if not permission.repair_tail or permission.durable_size or path.name in self.state.baseline:
                        raise ValueError(f"WARC contains no captures: {path}")
                    path.unlink()
                    sync_directory(self.path)
                else:
                    self.scans[path.name] = scanned
                    if scanned.size > permission.durable_size:
                        recovered.append(path)
                if scanned.repaired:
                    emit(f"year {self.year}: repaired incomplete WARC tail: {path.name}")
            # Inventory may only consume an index after its recovered bytes are durable.
            self.checkpoint(recovered)
            prepared.install()
        if not paths:
            self.layout.collection_index(self.year).unlink(missing_ok=True)
            sync_directory(self.path)
        inventory = inventory_collection(self.layout, self.year)
        if self.resumed:
            emit(f"year {self.year}: resumed {len(inventory.identities)} captures "
                 f"from {len(self.scans)} shards")
        return inventory

    @property
    def promotion_ready(self) -> bool:
        return (self.path / "ready.json").is_file()

    def checkpoint(self, paths: Sequence[Path]) -> None:
        """Sync all URL-group bytes before atomically advancing durable offsets."""
        if not paths:
            return
        sizes = dict(self.state.durable_sizes)
        for path in paths:
            sync_file(path)
            sizes[path.name] = path.stat().st_size
        sync_directory(self.path)
        previous = self.state.durable_sizes
        self.state.durable_sizes = sizes
        try:
            self._save()
        except BaseException:
            self.state.durable_sizes = previous
            raise

    def prepare_commit(self, changed_warcs: Sequence[Path] | None = None) -> YearChanges:
        """Index this invocation's writes and describe all retained changes once."""
        with prepare_collection_index(self.layout, self.year, changed_warcs=changed_warcs) as prepared:
            self.scans.update({path.name: scan for path, scan in prepared.scans.items()})
            index = prepared.install()
        warcs = []
        changed = []
        for path in list_collection_warcs(self.layout, self.year):
            scanned = self.scans[path.name]
            if scanned.size != path.stat().st_size:
                raise ValueError(f"WARC changed after indexing: {path}")
            artifact = warc_artifact_from_path(
                self.layout, path, collection_id=self.year, record_count=scanned.records,
            )
            warcs.append(artifact)
            if self.reset or _artifact_description(artifact) != self.state.baseline.get(path.name):
                changed.append(artifact)
        index_changed = index is not None and (
            self.reset or _artifact_description(index) != self.state.baseline.get(index.path.name)
        )
        names = {item.path.name for item in warcs}
        deleted = tuple(path.name for path in list_collection_warcs(self.canonical, self.year)
                        if self.reset and path.name not in names)
        return YearChanges(tuple(warcs), index, tuple(changed), index_changed, deleted)

    def abort(self) -> None:
        """Retain acquisition bytes; only explicit reset or commit discards work."""
        emit(f"year {self.year}: unfinished work retained at {self.path}")

    def discard(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
            sync_directory(self.path.parent)
        parent = self.path.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            sync_directory(parent.parent)

    def commit(self, changes: YearChanges) -> None:
        """Record a recoverable transaction, then install WARC bytes before index."""
        if not changes.artifacts and not self.reset:
            self.discard()
            return
        manifest = {
            "year": self.year,
            "reset": self.reset,
            "warcs": [_artifact_description(item) for item in changes.changed_warcs],
            "index": _artifact_description(changes.index) if changes.index_changed else None,
            "delete": list(changes.deleted),
        }
        self.checkpoint([item.path for item in changes.changed_warcs])
        if changes.index_changed and changes.index is not None:
            sync_file(changes.index.path)
        write_json_durably(self.path / "ready.json", manifest)
        _promote(self.canonical, self.path, manifest)
        if self.path.parent.is_dir() and not any(self.path.parent.iterdir()):
            self.path.parent.rmdir()
            sync_directory(self.canonical.root)


def _artifact_description(artifact: WarcArtifact | IndexArtifact) -> dict[str, object]:
    return {"name": artifact.path.name, "size": artifact.size_bytes, "sha256": artifact.sha256}


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
            sync_directory(canonical.root)
            sync_directory(stage)
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
            sync_directory(canonical.root)
            sync_directory(stage)
        elif not _matches(target, index):
            raise ValueError(f"cannot recover promoted index: {name}")
    for name in manifest["delete"]:
        (canonical.root / str(name)).unlink(missing_ok=True)
    sync_directory(canonical.root)
    shutil.rmtree(stage)
    sync_directory(stage.parent)
