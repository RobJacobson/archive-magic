"""Archive locking and atomic filesystem primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archive_magic_fetch.archive.layout import ArchiveLayout
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from archive_magic_fetch.models import PublicationError


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


def sync_file(path: Path) -> None:
    """Make a closed file's bytes durable before publishing a checkpoint."""
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def sync_directory(path: Path) -> None:
    """Persist directory entries after file creation, replacement, or removal."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def mkdir_durably(path: Path) -> None:
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.mkdir(parents=True, exist_ok=True)
    for directory in reversed(missing):
        sync_directory(directory.parent)


def write_json_durably(path: Path, value: object) -> None:
    """Install complete checkpoint metadata after syncing its contents."""
    mkdir_durably(path.parent)
    temporary = exclusive_temp_path(path.parent, suffix=".json.tmp")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        publish_file_atomically(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_file_atomically(source: Path, destination: Path) -> None:
    """Atomically replace destination with a complete source file."""

    mkdir_durably(destination.parent)
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
            sync_file(tmp_path)
            os.replace(tmp_path, destination)
            sync_directory(destination.parent)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
        source.unlink(missing_ok=True)
        return
    sync_file(source)
    os.replace(source, destination)
    sync_directory(destination.parent)


def exclusive_temp_path(directory: Path, *, suffix: str) -> Path:
    """Return an exclusive temporary path in directory."""

    mkdir_durably(directory)
    fd, name = tempfile.mkstemp(prefix=".tmp-", suffix=suffix, dir=directory)
    os.close(fd)
    path = Path(name)
    path.unlink()
    return path


@contextmanager
def archive_lock(layout: ArchiveLayout, directory: Path | None = None) -> Iterator[None]:
    """Prevent concurrent fetch, reset, and manual publication for one archive."""

    directory = directory or layout.logs_root
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".archive-magic.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublicationError("another fetch or sync owns this archive") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
