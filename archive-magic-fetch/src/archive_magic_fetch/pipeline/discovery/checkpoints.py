"""Private discovery checkpoints, separate from portable published caches."""

from pathlib import Path
import shutil

from archive_magic_fetch.runtime.manage_archive_files import sync_directory


def progress_root(index_directory: Path, checkpoint_directory: Path | None = None) -> Path:
    return checkpoint_directory or index_directory.parent / ".state" / "discovery"


def clear_progress(path: Path, root: Path) -> None:
    """Remove a completed or obsolete unit, pruning its empty namespace."""
    if path.exists():
        shutil.rmtree(path)
        sync_directory(path.parent)
    parent = path.parent
    while parent.is_relative_to(root) and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        sync_directory(parent.parent)
        parent = parent.parent
