"""Bounded URL scheduling with one persistent, explicitly instrumented client per worker."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from typing import Generic, TypeVar

from archive_magic_fetch.contracts import SourceAdapter
from archive_magic_fetch.runtime.track_http_requests import RequestStats
from archive_magic_fetch.runtime.pace_requests import check_cancelled

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

    def batch(self) -> CaptureBatch:
        return CaptureBatch(self._executor, self.max_workers)

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


class CaptureBatch:
    """Bounded ordered work that is cancelled and drained before its scope exits."""

    def __init__(self, executor: ThreadPoolExecutor, max_workers: int):
        self.cancelled = threading.Event()
        self._executor = executor
        self._max_workers = max_workers
        self._pending: deque[Future] = deque()
        self._failure: BaseException | None = None
        self._failure_lock = threading.Lock()

    def __enter__(self) -> CaptureBatch:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cancelled.set()
        for future in self._pending:
            future.cancel()
        wait(self._pending)
        self._pending.clear()
        if exc_type is None and self._failure is not None:
            raise self._failure

    def map(
        self, process: Callable[[Item], Result], groups: Sequence[Item]
    ) -> Iterator[Result]:
        next_submit = 0
        try:
            for _ in groups:
                while (
                    next_submit < len(groups)
                    and len(self._pending) < self._max_workers
                ):
                    check_cancelled(self.cancelled)
                    self._pending.append(
                        self._executor.submit(
                            self._process, process, groups[next_submit]
                        )
                    )
                    next_submit += 1
                # Keep the future owned by the batch if result() is interrupted.
                result = self._pending[0].result()
                self._pending.popleft()
                yield result
        except CancelledError:
            if self._failure is not None:
                raise self._failure
            raise

    def _process(self, process: Callable[[Item], Result], group: Item) -> Result:
        try:
            check_cancelled(self.cancelled)
            return process(group)
        except CancelledError:
            raise
        except BaseException as error:
            with self._failure_lock:
                if self._failure is None:
                    self._failure = error
            self.cancelled.set()
            raise
