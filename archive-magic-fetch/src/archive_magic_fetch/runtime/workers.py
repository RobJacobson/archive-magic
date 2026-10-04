"""Bounded URL scheduling with one persistent, explicitly instrumented client per worker."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from typing import Generic, TypeVar

from archive_magic_fetch.contracts import SourceAdapter
from archive_magic_fetch.runtime.http import RequestStats

Client = TypeVar("Client")
Item = TypeVar("Item")
Result = TypeVar("Result")


class CaptureWorkers(Generic[Client]):
    def __init__(
        self, source: SourceAdapter[Client], stats: RequestStats, *, max_workers: int
    ):
        self.source = source
        self.stats = stats
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="playback"
        )
        self._local = threading.local()
        self._owners = ExitStack()
        self._owners_lock = threading.Lock()

    def map(
        self, process: Callable[[Item], Result], groups: Sequence[Item]
    ) -> Iterator[Result]:
        pending: dict[int, Future[Result]] = {}
        next_submit = 0
        for index in range(len(groups)):
            while next_submit < len(groups) and len(pending) < self.max_workers:
                pending[next_submit] = self._executor.submit(
                    process, groups[next_submit]
                )
                next_submit += 1
            yield pending.pop(index).result()

    def client(self) -> Client:
        if not hasattr(self._local, "client"):
            # Opening a client does no network I/O; protect only lifecycle registration.
            with self._owners_lock:
                self._local.client = self._owners.enter_context(
                    self.source.open_client(self.stats)
                )
        return self._local.client

    def close(self) -> None:
        self._executor.shutdown()
        self._owners.close()
