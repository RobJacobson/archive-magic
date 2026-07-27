"""Thread-safe console formatting for concurrent export work."""

from __future__ import annotations

import io
import sys
import threading
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Iterator, Optional, TextIO
from urllib.parse import urlsplit


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
    """Mirror all process console writes during one CLI job."""

    mirror = ConsoleMirror()
    stdout = _MirroredTextIO(sys.stdout, mirror)
    stderr = _MirroredTextIO(sys.stderr, mirror)
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            yield mirror
    finally:
        mirror.close()


def readable_url(original_url: str) -> str:
    """Return a compact, non-SURT URL label for one capture group."""

    parsed = urlsplit(original_url)
    host = parsed.hostname or parsed.netloc
    if host.lower().startswith("www."):
        host = host[4:]

    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None and not (
        (parsed.scheme.lower() == "http" and port == 80)
        or (parsed.scheme.lower() == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{host}{path}{query}"


def capture_result_line(capture: object, result: str) -> str:
    """Format a capture URL and its result with scheme-aware alignment."""

    view_url = getattr(capture, "view_url", str(capture))
    original_url = getattr(capture, "original", "")
    scheme = (
        urlsplit(original_url).scheme.lower()
        if isinstance(original_url, str)
        else ""
    )
    separator = "  : " if scheme == "http" else " : "
    return f"{view_url}{separator}{result}"


def print_progress(message: str) -> None:
    """Print one immediate line without interleaving another output block."""

    with _OUTPUT_LOCK:
        print(message, flush=True)


class GroupReporter:
    """Emit completed URL-group blocks atomically in completion order."""

    def __init__(self, total: int) -> None:
        self._total = total
        self._completed = 0

    def emit(
        self,
        original_url: str,
        lines: list[str],
        summary: str,
    ) -> None:
        """Print one completed group and assign its completion number."""

        with _OUTPUT_LOCK:
            self._completed += 1
            block = [
                (
                    f"[completed {self._completed}/{self._total}] "
                    f"{readable_url(original_url)}"
                ),
                *lines,
                summary,
            ]
            print("\n".join(block), flush=True)
