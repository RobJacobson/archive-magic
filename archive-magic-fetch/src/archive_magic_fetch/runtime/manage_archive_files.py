"""Archive locking and atomic filesystem primitives."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archive_magic_fetch.archive.layout import ArchiveLayout
import fcntl
import hashlib
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


@contextmanager
def archive_lock(layout: ArchiveLayout) -> Iterator[None]:
    """Prevent concurrent fetch, reset, and manual publication for one archive."""

    layout.logs_root.mkdir(parents=True, exist_ok=True)
    with (layout.logs_root / ".archive-magic.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublicationError("another fetch or sync owns this archive") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
