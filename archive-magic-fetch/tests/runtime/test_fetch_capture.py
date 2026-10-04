"""Behavioral coverage for runtime / test_acquisition."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from archive_magic_fetch.adapters.interpret_wayback_failures import (
    INVALID_URI_PAYLOAD_DIGEST,
    backpressure_signal,
    pauses_playback_pool,
)
from archive_magic_fetch.models import FailureCategory
from archive_magic_fetch.runtime.pace_requests import StartGate
from helpers import AcquisitionHarness, make_capt, make_source, playback


def test_worker_retry_uses_five_then_ten_seconds():
    identity = make_capt()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return playback(capture)

    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(), download=download, sleep=sleeps.append
        ),
        sleep=sleeps.append,
        pace=False,
    )
    try:
        outcome = workers.download(identity)
    finally:
        workers.close()

    assert outcome.result is not None
    assert outcome.failure is None
    assert attempts == 3
    assert sleeps == [5.0, 10.0]


def test_rate_gate_keeps_maximum_retry_after(capsys):
    clock = {"now": 100.0}
    sleeps: list[float] = []
    identity = make_capt()

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(
        0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    # Same wave: level 1 floor is 60s; a longer Retry-After still wins.
    gate.pause("http", 30, identity, label="HTTP 429")
    gate.pause("http", 90, identity)
    gate.pause("http", 20, identity)
    gate.wait()

    assert sleeps == [90.0]
    output = capsys.readouterr().out
    assert output.startswith("HTTP 429 at ")
    assert "cooldown=60s, level=1; new starts paused for 90s" in output
    assert "Retry-After" not in output


def test_rate_gate_escalates_across_waves(capsys):
    clock = {"now": 100.0}
    sleeps: list[float] = []
    first = make_capt(ts="20040615000001")
    second = make_capt(ts="20040615000002")
    third = make_capt(ts="20040615000003")

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(
        0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    gate.pause("http", 60, first)
    gate.wait()
    gate.pause("http", 60, second)
    gate.wait()
    gate.pause("http", 60, third)
    gate.wait()

    assert sleeps == [60.0, 120.0, 180.0]
    output = capsys.readouterr().out
    assert "cooldown=60s, level=1" in output
    assert "cooldown=120s, level=2" in output
    assert "cooldown=180s, level=3" in output


def test_rate_gate_same_identity_escalates_after_each_cooldown(capsys):
    clock = {"now": 100.0}
    sleeps: list[float] = []
    identity = make_capt()

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(
        0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    gate.pause("http", 60, identity)
    gate.wait()
    gate.pause("http", 60, identity)
    gate.wait()
    gate.pause("http", 60, identity)
    gate.wait()

    assert sleeps == [60.0, 120.0, 180.0]
    output = capsys.readouterr().out
    assert "cooldown=120s, level=2" in output
    assert "cooldown=180s, level=3" in output


def test_rate_gate_concurrent_failures_share_level_but_next_retry_escalates(capsys):
    clock = {"now": 100.0}
    sleeps: list[float] = []
    first = make_capt(ts="20040615000001")
    second = make_capt(ts="20040615000002")

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(
        0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    gate.pause("http", 60, first)
    gate.pause("http", 60, second)
    gate.wait()
    gate.pause("http", 60, first)
    gate.wait()

    assert sleeps == [60.0, 120.0]
    output = capsys.readouterr().out
    assert output.count("level=1;") == 2
    assert output.count("level=2;") == 1


@pytest.mark.parametrize("healthy_seconds, next_delay", [(0, 180), (299, 180), (300, 60)])
def test_rate_gate_reset_requires_five_minutes_of_success(healthy_seconds, next_delay):
    clock = {"now": 100.0}
    sleeps: list[float] = []
    first = make_capt(ts="20040615000001")
    second = make_capt(ts="20040615000002")
    third = make_capt(ts="20040615000003")

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(
        0,
        clock=lambda: clock["now"],
        sleep=sleep,
    )
    gate.pause("http", 60, first)
    gate.wait()
    gate.pause("http", 60, second)
    gate.wait()
    gate.note_success()
    clock["now"] += healthy_seconds
    gate.note_success()
    gate.pause("http", 60, third)
    gate.wait()

    assert sleeps == [60.0, 120.0, next_delay]


def test_rate_gate_failure_restarts_recovery_window():
    clock = {"now": 100.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(0, clock=lambda: clock["now"], sleep=sleep)
    identity = make_capt()
    gate.pause("http", None, identity)
    gate.wait()
    gate.note_success()
    clock["now"] += 299
    gate.note_failure()
    clock["now"] += 1
    gate.note_success()
    gate.pause("http", None, identity)
    gate.wait()

    assert sleeps == [60.0, 120.0]


def test_rate_gate_cooldown_and_idle_time_do_not_count_as_recovery():
    clock = {"now": 100.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(0, clock=lambda: clock["now"], sleep=sleep)
    identity = make_capt()
    gate.pause("http", 600, identity)
    # A completion from an in-flight worker cannot start recovery during a pause.
    gate.note_success()
    gate.wait()
    clock["now"] += 600
    gate.note_success()
    gate.pause("http", None, identity)
    gate.wait()

    assert sleeps == [600.0, 120.0]


def test_rate_gate_caps_policy_delay_but_honors_longer_retry_after():
    clock = {"now": 100.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    gate = StartGate(0, clock=lambda: clock["now"], sleep=sleep)
    identity = make_capt()
    for _ in range(12):
        gate.pause("http", None, identity)
        gate.wait()
    gate.pause("http", 900, identity)
    gate.wait()
    gate.pause("http", None, identity)
    gate.wait()

    assert sleeps == [60.0 * level for level in range(1, 11)] + [600, 600, 900, 600]


def test_permanent_failure_does_not_retry():
    download = MagicMock(side_effect=RuntimeError("permanent"))
    sleeps: list[float] = []

    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(), download=download, sleep=sleeps.append
        ),
        sleep=sleeps.append,
        pace=False,
    )
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert outcome.result is None
    assert outcome.failure is not None
    assert download.call_count == 1
    assert sleeps == []


def test_invalid_uri_digest_skipsplayback():
    download = MagicMock(side_effect=AssertionError("should not download"))
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=download,
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
    )
    try:
        outcome = workers.download(make_capt(digest=INVALID_URI_PAYLOAD_DIGEST))
    finally:
        workers.close()

    assert outcome.result is None
    assert outcome.failure is not None
    download.assert_not_called()


def test_connection_refused_is_tcp_backpressure():
    error = ConnectionError("Max retries exceeded: [Errno 61] Connection refused")
    assert backpressure_signal(error) == (
        "tcp",
        60.0,
    )


def test_read_timeout_is_timeout_backpressure():
    error = TimeoutError(
        "HTTPSConnectionPool(host='web.archive.org', port=443): Read timed out."
    )
    assert backpressure_signal(error) == ("timeout", None)


def test_remote_disconnected_is_timeout_backpressure():
    error = ConnectionError(
        "('Connection aborted.', RemoteDisconnected("
        "'Remote end closed connection without response'))"
    )
    assert backpressure_signal(error) == ("timeout", None)


def test_only_http_and_tcp_pause_the_playback_pool():
    assert pauses_playback_pool("http") is True
    assert pauses_playback_pool("tcp") is True
    assert pauses_playback_pool("timeout") is False
    assert pauses_playback_pool(None) is False


def test_playback_timeout_retries_once_without_pausing_pool(capsys):
    identity = make_capt()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("Read timed out.")
        return playback(capture)

    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(), download=download, sleep=sleeps.append
        ),
        sleep=sleeps.append,
        pace=False,
    )
    try:
        outcome = workers.download(identity)
    finally:
        workers.close()

    assert outcome.result is not None
    assert outcome.failure is None
    assert attempts == 2
    assert sleeps == [5.0]
    assert "new starts paused" not in capsys.readouterr().out


def test_second_playback_timeout_is_terminal(capsys):
    download = MagicMock(side_effect=TimeoutError("Read timed out."))
    sleeps: list[float] = []
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(), download=download, sleep=sleeps.append
        ),
        sleep=sleeps.append,
        pace=False,
    )
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert outcome.result is None
    assert outcome.failure is not None
    assert outcome.failure.category is FailureCategory.RETRY_EXHAUSTED
    assert outcome.attempts == 2
    assert download.call_count == 2
    assert sleeps == [5.0]
    assert "new starts paused" not in capsys.readouterr().out


def test_http_504_retries_once_without_pausing_pool(capsys):
    attempts = 0

    def download(_client, _capture):
        nonlocal attempts
        attempts += 1
        error = RuntimeError("504 Gateway Timeout")
        error.status_code = 504
        raise error

    sleeps: list[float] = []
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(), download=download, sleep=sleeps.append
        ),
        sleep=sleeps.append,
        pace=False,
    )
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert attempts == 2
    assert outcome.failure is not None
    assert sleeps == [5.0]
    assert "new starts paused" not in capsys.readouterr().out


def test_unavailable_failure_interrupts_recovery_without_resetting_rate_gate():
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=MagicMock(side_effect=RuntimeError("permanent")),
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
        retries=0,
    )
    workers._gate.note_success = MagicMock(wraps=workers._gate.note_success)
    workers._gate.note_failure = MagicMock(wraps=workers._gate.note_failure)
    workers._gate.pause = MagicMock(wraps=workers._gate.pause)
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert outcome.failure is not None
    workers._gate.pause.assert_not_called()
    workers._gate.note_success.assert_not_called()
    workers._gate.note_failure.assert_called_once()


def test_http_429_pauses_pool_without_reset():
    def download(_client, _capture):
        error = RuntimeError("429 Too Many Requests")
        error.status_code = 429
        raise error

    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=download,
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
        retries=0,
    )
    workers._gate.note_success = MagicMock()
    workers._gate.pause = MagicMock(wraps=workers._gate.pause)
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert outcome.failure is not None
    workers._gate.pause.assert_called_once()
    workers._gate.note_success.assert_not_called()


def test_playback_workers_run_url_groups_in_parallel():
    barrier = threading.Barrier(4)
    threads: set[str] = set()
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
    )

    def process(group):
        threads.add(threading.current_thread().name)
        barrier.wait(timeout=2)
        return group[0]

    try:
        results = list(workers.map(process, [[1], [2], [3], [4]]))
    finally:
        workers.close()

    assert set(results) == {1, 2, 3, 4}
    assert len(threads) == 4


def test_every_url_group_is_walked_once_by_a_worker():
    main_thread = threading.current_thread().name
    seen: list[tuple[int, str]] = []
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
        max_workers=1,
    )

    def process(group):
        seen.append((group[0], threading.current_thread().name))
        return group[0]

    try:
        results = list(workers.map(process, [[1], [2], [3]]))
    finally:
        workers.close()

    assert results == [1, 2, 3]
    assert [number for number, _thread in seen] == [1, 2, 3]
    assert all(thread != main_thread for _number, thread in seen)


def test_url_scheduling_remains_bounded_without_a_prepass():
    started_downloads: list[int] = []
    workers = AcquisitionHarness(
        make_source(
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
        sleep=lambda _seconds: None,
        pace=False,
        max_workers=1,
    )

    def process(group):
        n = group[0]
        if n in {2, 5}:
            started_downloads.append(n)
        return n

    try:
        iterator = workers.map(process, [[1], [2], [3], [4], [5]])
        assert next(iterator) == 1
        assert next(iterator) == 2
        assert started_downloads == [2]
        assert next(iterator) == 3
        assert started_downloads == [2]
        assert next(iterator) == 4
        assert started_downloads == [2]
        assert next(iterator) == 5
        assert started_downloads == [2, 5]
    finally:
        workers.close()
