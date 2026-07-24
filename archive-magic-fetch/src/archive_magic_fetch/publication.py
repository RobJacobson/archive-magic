"""Race-safe same-filesystem publication helpers."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


_AT_FDCWD = -100
_LINUX_RENAME_NOREPLACE = 1
_MACOS_RENAME_EXCL = 0x00000004


def _ensure_siblings(source: Path, destination: Path) -> None:
    if source.parent.resolve() != destination.parent.resolve():
        raise ValueError("temporary and final paths must be sibling entries")


def _raise_rename_error(error_number: int, destination: Path) -> None:
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            destination,
        )
    raise OSError(error_number, os.strerror(error_number), destination)


def _linux_rename_noreplace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise NotImplementedError(
            "atomic no-replace path publication requires renameat2"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _LINUX_RENAME_NOREPLACE,
    )
    if result != 0:
        _raise_rename_error(ctypes.get_errno(), destination)


def _macos_rename_noreplace(source: Path, destination: Path) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(library, "renamex_np", None)
    if renamex_np is None:
        raise NotImplementedError(
            "atomic no-replace path publication requires renamex_np"
        )
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(
        os.fsencode(source),
        os.fsencode(destination),
        _MACOS_RENAME_EXCL,
    )
    if result != 0:
        _raise_rename_error(ctypes.get_errno(), destination)


def _publish_path_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a complete sibling entry without replacement."""

    _ensure_siblings(source, destination)
    if os.name == "nt":
        os.rename(source, destination)
    elif sys.platform == "darwin":
        _macos_rename_noreplace(source, destination)
    elif sys.platform.startswith("linux"):
        _linux_rename_noreplace(source, destination)
    else:
        raise NotImplementedError(
            "atomic no-replace path publication is unsupported "
            f"on {sys.platform}"
        )


def publish_directory_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a complete sibling directory without replacement."""

    _publish_path_noreplace(source, destination)


def publish_file_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a complete sibling file without replacement."""

    _ensure_siblings(source, destination)
    try:
        os.link(source, destination)
    except FileExistsError:
        raise
    except OSError:
        _publish_path_noreplace(source, destination)
        return
    source.unlink()
