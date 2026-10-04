"""Run one complete acquisition, including decoding, under the source policy."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from typing import TypeVar

from archive_magic_fetch.contracts import SourceAdapter
from archive_magic_fetch.models import CaptureRef, DownloadOutcome, UnresolvedFailure
from archive_magic_fetch.runtime.http import RequestStats
from archive_magic_fetch.runtime.pacing import StartGate

Client = TypeVar("Client")


def fetch_capture(
    capture: CaptureRef,
    *,
    source: SourceAdapter[Client],
    client: Callable[[], Client],
    gate: StartGate,
    stats: RequestStats,
    max_attempts: int,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadOutcome:
    failure = source.preflight(capture)
    if failure is not None:
        return DownloadOutcome(None, failure, 0, 0.0, ())
    started = clock()
    categories: list[str] = []
    failures: Counter[str] = Counter()
    for attempt in range(1, max_attempts + 1):
        try:
            worker_client = client()
            with stats.attempt(capture.identity, attempt):
                result = source.fetch(worker_client, capture)
        except Exception as error:
            advice = source.failure_advice(error, attempt)
            categories.append(advice.category.value)
            retryable = advice.retryable
            if advice.group is not None:
                failures[advice.group] += 1
                if (
                    advice.group_limit is not None
                    and failures[advice.group] >= advice.group_limit
                ):
                    retryable = False
            if advice.cooldown is not None:
                kind, delay, label = advice.cooldown
                gate.pause(kind, delay, capture.identity, label=label)
            elif advice.reset_gate:
                gate.note_success()
            if retryable and attempt < max_attempts:
                if advice.cooldown is None:
                    sleep(advice.delay)
                continue
            return DownloadOutcome(
                None,
                UnresolvedFailure(
                    capture.identity,
                    advice.category,
                    str(error) or type(error).__name__,
                ),
                attempt,
                clock() - started,
                tuple(categories),
            )
        gate.note_success()
        return DownloadOutcome(
            result, None, attempt, clock() - started, tuple(categories)
        )
    raise AssertionError("acquisition retry loop did not terminate")
