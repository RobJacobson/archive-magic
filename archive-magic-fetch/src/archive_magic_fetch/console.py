"""Thread-safe console output and source-log mirroring."""

from __future__ import annotations

import io
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator, Optional, TextIO


_OUTPUT_LOCK = threading.Lock()


class ConsoleMirror:
    """Mirror stdout and stderr into one attachable UTF-8 log."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending = io.StringIO()
        self._log: Optional[TextIO] = None

    def write(self, stream: TextIO, text: str) -> int:
        """Write to the original stream and the attached or pending log."""

        with self._lock:
            written = stream.write(text)
            if self._log is None:
                self._pending.write(text)
            else:
                self._log.write(text)
                self._log.flush()
            return written

    def flush(self, stream: TextIO) -> None:
        """Flush both the original stream and an attached log."""

        with self._lock:
            stream.flush()
            if self._log is not None:
                self._log.flush()

    def attach(self, path: Path) -> None:
        """Create the final log and flush all earlier console output into it."""

        with self._lock:
            if self._log is not None:
                raise RuntimeError("console log is already attached")
            self._log = path.open("x", encoding="utf-8", buffering=1)
            self._log.write(self._pending.getvalue())
            self._log.flush()
            self._pending.close()

    def close(self) -> None:
        """Flush and close the attached log, if any."""

        with self._lock:
            if self._log is not None:
                self._log.close()
                self._log = None
            if not self._pending.closed:
                self._pending.close()


class _MirroredTextIO:
    """Text stream proxy backed by one shared console mirror."""

    def __init__(self, stream: TextIO, mirror: ConsoleMirror) -> None:
        self._stream = stream
        self._mirror = mirror

    def write(self, text: str) -> int:
        return self._mirror.write(self._stream, text)

    def flush(self) -> None:
        self._mirror.flush(self._stream)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


@contextmanager
def mirror_console_output() -> Iterator[ConsoleMirror]:
    """Mirror all process console writes during one CLI request."""

    mirror = ConsoleMirror()
    stdout = _MirroredTextIO(sys.stdout, mirror)
    stderr = _MirroredTextIO(sys.stderr, mirror)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield mirror
    finally:
        mirror.close()


def print_progress(message: str) -> None:
    """Print one immediate line without interleaving another output block."""

    with _OUTPUT_LOCK:
        print(message, flush=True)
