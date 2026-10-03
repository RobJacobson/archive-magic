"""Playback workers and CLI argument contract."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from archive_magic_fetch.models import FailureCategory
from archive_magic_fetch.protocol import (
    INVALID_URI_PAYLOAD_DIGEST,
)
from archive_magic_fetch.resolution import (
    iter_url_outcomes,
)
from archive_magic_fetch.retry import backpressure_signal, pauses_playback_pool
from archive_magic_fetch.workers import (
    PlaybackWorkers,
    StartGate,
)
from helpers import make_capt, playback


def write_cli_config(
    directory: Path,
    *,
    output_type: str = "local",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    remote = ""
    if output_type == "remote":
        remote = """
bucket = "bucket"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"
"""
    path = directory / "fetch.toml"
    path.write_text(
        f"""
[archive]
id = "example.org"
url_pattern = "*.example.org"
[output]
type = "{output_type}"
data_directory = "data"
{remote}
[fetch]
start = "2000-01-01"
end = "2001-12-31"
""",
        encoding="utf-8",
    )
    return path


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

    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    gate.pause("http", 30, identity)
    gate.pause("http", 90, identity)
    gate.pause("http", 20, identity)
    gate.wait()

    assert sleeps == [90.0]
    output = capsys.readouterr().out
    assert output.startswith("HTTP 429 at ")
    assert "Retry-After=20s, applied=60s, level=1, maximum=90s" in output


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
    assert "level=1, maximum=60s" in output
    assert "level=2, maximum=120s" in output
    assert "applied=120s, level=2, maximum=120s" in output
    assert "level=3, maximum=180s" in output


def test_rate_gate_same_identity_does_not_escalate(capsys):
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

    assert sleeps == [60.0, 60.0, 60.0]
    output = capsys.readouterr().out
    assert output.count("level=1,") == 3
    assert "level=2" not in output


def test_rate_gate_wave_retries_do_not_escalate(capsys):
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

    assert sleeps == [60.0, 60.0]
    output = capsys.readouterr().out
    assert "level=2" not in output


def test_rate_gate_resets_escalation_after_success(capsys):
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
    gate.pause("http", 60, third)
    gate.wait()

    assert sleeps == [60.0, 120.0, 60.0]
    output = capsys.readouterr().out
    assert output.count("level=1,") == 2
    assert "level=2, maximum=120s" in output


def test_permanent_failure_does_not_retry():
    download = MagicMock(side_effect=RuntimeError("permanent"))
    sleeps: list[float] = []

    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    error = ConnectionError(
        "Max retries exceeded: [Errno 61] Connection refused"
    )
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

    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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


def test_unavailable_failure_resets_rate_gate():
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        MagicMock(side_effect=RuntimeError("permanent")),
        sleep=lambda _seconds: None,
        pace=False,
        retries=0,
    )
    workers._gate.note_success = MagicMock(wraps=workers._gate.note_success)
    workers._gate.pause = MagicMock(wraps=workers._gate.pause)
    try:
        outcome = workers.download(make_capt())
    finally:
        workers.close()

    assert outcome.failure is not None
    workers._gate.pause.assert_not_called()
    workers._gate.note_success.assert_called_once()


def test_http_429_pauses_pool_without_reset():
    def download(_client, _capture):
        error = RuntimeError("429 Too Many Requests")
        error.status_code = 429
        raise error

    workers = PlaybackWorkers(
        lambda: MagicMock(),
        download,
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
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        lambda _client, identity: playback(identity),
        sleep=lambda _seconds: None,
        pace=False,
    )

    def process(group):
        threads.add(threading.current_thread().name)
        barrier.wait(timeout=2)
        return group[0]

    try:
        results = list(
            iter_url_outcomes(
                [[1], [2], [3], [4]],
                process,
                workers,
                (False, False, False, False),
            )
        )
    finally:
        workers.close()

    assert set(results) == {1, 2, 3, 4}
    assert len(threads) == 4


def test_represented_url_groups_skip_playback_workers():
    main_thread = threading.current_thread().name
    seen: list[tuple[int, str]] = []
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        lambda _client, identity: playback(identity),
        sleep=lambda _seconds: None,
        pace=False,
        max_workers=1,
    )

    def process(group):
        seen.append((group[0], threading.current_thread().name))
        return group[0]

    try:
        results = list(
            iter_url_outcomes(
                [[1], [2], [3]],
                process,
                workers,
                (True, False, True),
            )
        )
    finally:
        workers.close()

    assert results == [1, 2, 3]
    assert seen[0] == (1, main_thread)
    assert seen[1][0] == 2
    assert seen[1][1] != main_thread
    assert seen[2] == (3, main_thread)


def test_skip_groups_yield_before_next_download_starts():
    started_downloads: list[int] = []
    workers = PlaybackWorkers(
        lambda: MagicMock(),
        lambda _client, identity: playback(identity),
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
        iterator = iter_url_outcomes(
            [[1], [2], [3], [4], [5]],
            process,
            workers,
            (True, False, True, True, False),
        )
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


def test_cli_startup_hides_pkg_resources_deprecation():
    completed = subprocess.run(
        [sys.executable, "-c", "import archive_magic_fetch.cli"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "pkg_resources is deprecated" not in completed.stderr
    assert "pkg_resources is deprecated" not in completed.stdout


def test_cli_rejects_reversed_range(tmp_path):
    from archive_magic_fetch.cli import main

    config = write_cli_config(tmp_path)
    code = main(
        [
            str(config),
            "--start",
            "20050101",
            "--end",
            "20040101",
        ]
    )
    assert code == 2


@pytest.mark.parametrize("data_directory", ["data", "../storage/data"])
def test_cli_uses_configured_history(tmp_path, monkeypatch, data_directory):
    from archive_magic_fetch import cli

    config = write_cli_config(tmp_path)
    config.write_text(config.read_text().replace(
        'data_directory = "data"', f'data_directory = "{data_directory}"'
    ))
    captured = []

    def run(settings):
        captured.append(settings)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(cli, "run_fetch", run)
    assert cli.main([str(config.parent)]) == 0
    assert captured[0].archive_id == "example.org"
    assert captured[0].date_start == "20000101000000"
    assert captured[0].date_end == "20011231235959"
    assert captured[0].output.data_directory == (tmp_path / data_directory).resolve()
    assert captured[0].index_directory == tmp_path / "index"


def test_remote_reset_rejects_dates_and_warns_before_full_rebuild(
    tmp_path,
    monkeypatch,
    capsys,
):
    from archive_magic_fetch import cli

    config = write_cli_config(tmp_path, output_type="remote")
    assert cli.main([str(config), "--reset-data", "--start", "2001"]) == 2
    assert "complete configured date range" in capsys.readouterr().err

    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda settings: SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(config), "--reset-data"]) == 0
    warning = capsys.readouterr().err
    assert "delete and rebuild managed remote archive data" in warning
    assert "playback will be unavailable" in warning


def test_cli_runtime_flags_override_defaults(tmp_path, monkeypatch):
    from archive_magic_fetch import cli

    config = write_cli_config(tmp_path)
    captured = []
    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda item: captured.append(item) or SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(config)]) == 0
    assert captured[0].playback_workers == 4
    assert captured[0].playback_starts_per_second == 16.0
    assert captured[0].retries == 4
    assert captured[0].trace_requests is False
    assert captured[0].cdx_window_days == 28
    assert captured[0].cdx_page_limit == 5000
    captured.clear()
    assert (
        cli.main(
            [
                str(config),
                "--workers",
                "2",
                "--starts-per-second",
                "1.5",
                "--retries",
                "0",
                "--trace-requests",
            ]
        )
        == 0
    )
    assert captured[0].playback_workers == 2
    assert captured[0].playback_starts_per_second == 1.5
    assert captured[0].retries == 0
    assert captured[0].trace_requests is True
    assert captured[0].cdx_window_days == 28
    assert captured[0].cdx_page_limit == 5000


def test_cli_uses_instance_fetch_config_and_cli_overrides(tmp_path, monkeypatch):
    from archive_magic_fetch import cli

    archive = write_cli_config(tmp_path)
    policy = tmp_path / "fetch-config.toml"
    policy.write_text(
        """
[playback]
workers = 3
starts_per_second = 8
retries = 6
""",
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda item: captured.append(item) or SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(archive), "--config", str(policy)]) == 0
    assert captured[0].playback_workers == 3
    assert captured[0].playback_starts_per_second == 8.0
    assert captured[0].retries == 6
    captured.clear()
    assert cli.main(
        [str(archive), "--config", str(policy), "--starts-per-second", "1.5"]
    ) == 0
    assert captured[0].playback_workers == 3
    assert captured[0].playback_starts_per_second == 1.5
    assert captured[0].retries == 6


def test_cli_uses_cdx_settings_from_toml(tmp_path, monkeypatch):
    from archive_magic_fetch import cli

    path = tmp_path / "fetch.toml"
    path.write_text(
        """
[archive]
id = "example.org"
url_pattern = "*.example.org"
[output]
type = "local"
data_directory = "data"
[fetch]
start = "2000-01-01"
end = "2004-12-31"
cdx_window_days = 3
cdx_page_limit = 2500
""",
        encoding="utf-8",
    )
    captured = []
    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda item: captured.append(item) or SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(path)]) == 0
    assert captured[0].cdx_window_days == 3
    assert captured[0].cdx_page_limit == 2500


def test_cli_rejects_start_before_project_range(tmp_path):
    from archive_magic_fetch.cli import main

    config = write_cli_config(tmp_path)
    assert main([str(config), "--start", "1999-01-01"]) == 2


def test_log_url_outcome_omits_already_represented_lines(capsys):
    from archive_magic_fetch.console import log_url_outcome
    from archive_magic_fetch.resolution import CaptureKind, CaptureOutcome, UrlOutcome

    existing = make_capt(ts="19990117001820")
    downloaded = make_capt(ts="19990117071312", digest="sha1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    outcome = UrlOutcome(
        url="http://www.nclr.org/special/award.html",
        captures=(
            CaptureOutcome(identity=existing, kind=CaptureKind.EXISTING),
            CaptureOutcome(
                identity=downloaded,
                kind=CaptureKind.DOWNLOADED,
                playback=playback(downloaded),
                attempts=1,
                elapsed_s=0.4,
            ),
        ),
        attempts=1,
        playback_bytes=5,
        categories=(),
    )

    log_url_outcome(1, 397, outcome)
    text = capsys.readouterr().out
    assert "1/397 http://www.nclr.org/special/award.html" in text
    assert "already represented" not in text
    assert "Downloaded" in text
