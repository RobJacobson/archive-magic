"""Polling and atomic publication of validated replay index snapshots."""

from __future__ import annotations

import heapq
import os
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path

from .collections import Archive


class IndexStore:
    """Lifecycle shared by stores with accepted `_states` and `_poll_archive`."""

    def __init__(self, cache_directory: Path, poll_seconds: float) -> None:
        self.cache_directory = cache_directory.expanduser().resolve()
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_polling(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="archive-magic-index-sync"
        )
        self._thread.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            for archive_id in tuple(self._states):
                try:
                    self._poll_archive(archive_id)
                except Exception as error:  # noqa: BLE001 - keep poller alive
                    print(
                        f"WARNING: index synchronization failed for {archive_id}: {error}",
                        file=sys.stderr,
                    )


def publish_indexes(archive: Archive, staged: list[tuple[Path, Path]]) -> None:
    """Merge annual CDXJ streams and switch playback only after all writes succeed.

    Annual cache files are recovery inputs; pywb reads only the merged snapshot.
    Open readers retain the previous inode across replacement. No WARC is copied.
    """
    destination = archive.replay_index
    assert destination is not None
    destination.parent.mkdir(parents=True, exist_ok=True)
    replacements = dict(staged)
    fd, name = tempfile.mkstemp(prefix=".tmp-replay-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, ExitStack() as stack:
            streams = [
                stack.enter_context(
                    replacements.get(item.replay_index, item.replay_index).open("rb")
                )
                for item in archive.collections
            ]
            # CDXJ indexes are sorted by URL key and timestamp, not JSON payload.
            lines = [
                (line.rstrip(b"\r\n") + b"\n" for line in stream if line.strip())
                for stream in streams
            ]
            output.writelines(
                heapq.merge(*lines, key=lambda line: line.split(maxsplit=2)[:2])
            )
        for target, source in staged:
            os.replace(source, target)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
