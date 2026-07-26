"""Thread-safe console formatting for concurrent export work."""

from __future__ import annotations

import threading
from urllib.parse import urlsplit


_OUTPUT_LOCK = threading.Lock()


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
