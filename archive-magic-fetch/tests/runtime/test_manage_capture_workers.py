"""Annual batch cancellation, draining, and transport boundaries."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from unittest.mock import MagicMock

import pytest
import requests
from requests.adapters import HTTPAdapter

from archive_magic_fetch.contracts import FailureAdvice
from archive_magic_fetch.models import CaptureRef, FailureCategory
from archive_magic_fetch.pipeline.retrieval.fetch_capture import fetch_capture
from archive_magic_fetch.runtime.track_http_requests import InstrumentedHTTPAdapter, RequestStats
from archive_magic_fetch.runtime.pace_requests import StartGate, check_cancelled
from archive_magic_fetch.runtime.manage_capture_workers import CaptureBatch
from helpers import make_capt, make_source


class ObservedEvent(Event):
    def __init__(self):
        super().__init__()
        self.waiting = Event()

    def wait(self, timeout=None):
        self.waiting.set()
        return super().wait(timeout)


def test_failed_worker_cancels_queued_groups_and_preserves_its_error(monkeypatch):
    failure = RuntimeError("worker failed")
    seen = []
    queued = Event()
    with ThreadPoolExecutor(max_workers=1) as executor:
        submit = executor.submit
        submitted = []

        def track_submit(*args):
            future = submit(*args)
            submitted.append(future)
            if len(submitted) == 2:
                queued.set()
            return future

        monkeypatch.setattr(executor, "submit", track_submit)
        # A batch of two guarantees that one group must queue behind the first.
        with pytest.raises(RuntimeError) as raised:
            with CaptureBatch(executor, 2) as batch:
                def process(group):
                    seen.append(group)
                    assert queued.wait(2)
                    raise failure

                list(batch.map(process, [1, 2, 3]))
    assert raised.value is failure
    assert seen == [1]


def test_out_of_order_worker_failure_stops_sibling_and_keeps_original_error():
    started = Event()
    finished = Event()
    failure = RuntimeError("second group failed")
    with ThreadPoolExecutor(max_workers=2) as executor:
        with pytest.raises(RuntimeError) as raised:
            with CaptureBatch(executor, 2) as batch:
                def process(group):
                    if group == 2:
                        assert started.wait(2)
                        raise failure
                    started.set()
                    assert batch.cancelled.wait(2)
                    finished.set()
                    check_cancelled(batch.cancelled)

                list(batch.map(process, [1, 2]))
        assert finished.is_set()
    assert raised.value is failure


@pytest.mark.parametrize("failure", [OSError("writer failed"), KeyboardInterrupt()])
def test_batch_drains_active_work_and_preserves_consumer_failure(failure):
    started = Event()
    settled = Event()
    with ThreadPoolExecutor(max_workers=2) as executor:
        with pytest.raises(type(failure)) as raised:
            with CaptureBatch(executor, 2) as batch:
                def process(group):
                    if group == 1:
                        assert started.wait(2)
                        return group
                    started.set()
                    assert batch.cancelled.wait(2)
                    settled.set()
                    raise RuntimeError("secondary cleanup failure")

                outcomes = batch.map(process, [1, 2, 3])
                assert next(outcomes) == 1
                raise failure
        assert settled.is_set()
    assert raised.value is failure


def test_cancelled_retry_wait_does_not_start_another_attempt():
    cancelled = ObservedEvent()
    fetch = MagicMock(side_effect=TimeoutError("retry me"))
    advice = MagicMock(
        return_value=FailureAdvice(FailureCategory.RETRY_EXHAUSTED, True, delay=60)
    )
    source = replace(make_source(), fetch=fetch, failure_advice=advice)
    gate = StartGate(0)
    stats = RequestStats(gate.wait, report=lambda _: None)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                fetch_capture,
                CaptureRef(make_capt(), "text/html"),
                source=source,
                client=object,
                gate=gate,
                stats=stats,
                max_attempts=5,
                cancelled=cancelled,
            )
            try:
                assert cancelled.waiting.wait(2)
            finally:
                cancelled.set()
            with pytest.raises(CancelledError):
                future.result(timeout=2)
    finally:
        stats.close()
    assert fetch.call_count == advice.call_count == 1


def test_cancelled_transport_wait_sends_nothing_and_retains_cooldown(
    tmp_path, monkeypatch
):
    cancelled = ObservedEvent()
    gate = StartGate(1, clock=lambda: 100.0, report=lambda _: None)
    gate.pause("http", 60, make_capt())
    trace = tmp_path / "trace.csv"
    stats = RequestStats(gate.wait, report=lambda _: None, trace_path=trace)
    advice = MagicMock(side_effect=AssertionError("cancellation is not a source failure"))
    send = MagicMock(side_effect=AssertionError("transport must not start"))
    monkeypatch.setattr(HTTPAdapter, "send", send)
    adapter = InstrumentedHTTPAdapter(stats)
    request = requests.Request("GET", "https://example.org/").prepare()
    source = replace(
        make_source(),
        fetch=lambda client, capture: adapter.send(request),
        failure_advice=advice,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                fetch_capture,
                CaptureRef(make_capt(), "text/html"),
                source=source,
                client=object,
                gate=gate,
                stats=stats,
                max_attempts=5,
                cancelled=cancelled,
            )
            try:
                assert cancelled.waiting.wait(2)
            finally:
                cancelled.set()
            with pytest.raises(CancelledError):
                future.result(timeout=2)
    finally:
        adapter.close()
        stats.close()
    send.assert_not_called()
    advice.assert_not_called()
    assert stats.snapshot()["requests_total"] == 0
    assert gate._blocked_until == 160.0
    assert len(trace.read_text().splitlines()) == 1
    assert stats._stream is None
