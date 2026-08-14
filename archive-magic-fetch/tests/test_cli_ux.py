"""Console formatting and playback retry presentation."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from archive_magic_fetch.fetch import (
    _format_elapsed,
    _log_url_outcome,
    _playback_timing,
    _style_result,
    _timestamp_link,
)
from archive_magic_fetch.protocol import (
    INVALID_URI_PAYLOAD_DIGEST,
)
from archive_magic_fetch.models import FailureCategory, UnresolvedFailure
from archive_magic_fetch.resolution import (
    CaptureKind,
    CaptureOutcome,
    UrlOutcome,
    iter_url_outcomes,
)
from archive_magic_fetch.workers import (
    PlaybackWorkers,
    StartGate,
    backpressure_signal,
)
from helpers import make_capt, playback


def write_cli_descriptor(
    directory: Path,
    *,
    authority: str = "local",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    remote = ""
    if authority == "remote":
        remote = """
[storage.remote]
bucket = "bucket"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"
"""
    path = directory / "archive.toml"
    path.write_text(
        f"""
schema_version = 1
[archive]
id = "example.org"
url_pattern = "*.example.org"
[storage]
authority = "{authority}"
workspace_directory = "workspace"
{remote}
[fetch]
start = "2000-01-01"
end = "2001-12-31"
""",
        encoding="utf-8",
    )
    return path


def test_console_timestamp_links_to_full_capture():
    identity = make_capt(
        url="http://www.example.org/a",
        ts="20080516181742",
    )

    plain = _timestamp_link(identity, enabled=False)
    linked = _timestamp_link(identity, enabled=True)

    assert plain == "2008-05-16T18:17:42"
    assert plain in linked
    assert "https://web.archive.org/web/20080516181742id_/http://www.example.org/a" in linked
    assert linked.startswith("\033]8;;")
    assert _style_result("Error", "error", enabled=False) == "Error"
    assert _style_result("Error", "error", enabled=True) == "\033[1;31mError\033[0m"


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
    gate.pause("http", 30, identity)
    gate.pause("http", 40, identity)
    gate.pause("http", 20, identity)
    gate.wait()

    assert sleeps == [40.0]
    output = capsys.readouterr().out
    assert "Retry-After=20s, applied=20s, maximum=40s" in output


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


def test_url_table_is_rendered_as_one_chronological_section(capsys):
    first = make_capt(ts="20040601000000")
    second = make_capt(ts="20040602000000")
    _log_url_outcome(
        1,
        2,
        UrlOutcome(
            url=first.original_url,
            captures=(
                CaptureOutcome(
                    first,
                    CaptureKind.DOWNLOADED,
                    playback=playback(first),
                    attempts=1,
                    elapsed_s=0.1,
                ),
                CaptureOutcome(second, CaptureKind.REVISIT),
            ),
            attempts=1,
            playback_bytes=5,
            categories=(),
        ),
    )

    output = capsys.readouterr().out
    assert output.count(first.original_url) == 1
    assert output.index("2004-06-01T00:00:00") < output.index(
        "2004-06-02T00:00:00"
    )
    assert first.payload_digest[-6:] in output
    assert "Capture              Digest  Result" in output


def test_playback_timing_includes_elapsed_and_attempts():
    once = CaptureOutcome(
        identity=make_capt(),
        kind=CaptureKind.DOWNLOADED,
        attempts=1,
        elapsed_s=1.24,
    )
    retried = CaptureOutcome(
        identity=make_capt(),
        kind=CaptureKind.DOWNLOADED,
        attempts=2,
        elapsed_s=6.0,
    )
    assert _playback_timing(once) == "1.2s"
    assert _playback_timing(retried) == "6.0s, 2 attempts"


def test_url_table_shows_elapsed_on_ignored_fetch(capsys):
    identity = make_capt(ts="20041009172745")
    _log_url_outcome(
        1,
        1,
        UrlOutcome(
            url=identity.original_url,
            captures=(
                CaptureOutcome(
                    identity,
                    CaptureKind.FAILURE,
                    failure=UnresolvedFailure(
                        identity,
                        FailureCategory.UNAVAILABLE,
                        "unavailable",
                    ),
                    attempts=1,
                    elapsed_s=1.24,
                ),
            ),
            attempts=1,
            playback_bytes=0,
            categories=(),
        ),
    )

    output = capsys.readouterr().out
    assert "Ignored [unavailable] (1.2s)" in output


def test_elapsed_format_uses_unbounded_hours():
    assert _format_elapsed(3661.9) == "01:01:01"
    assert _format_elapsed(25 * 60 * 60 + 2) == "25:00:02"


def test_cli_rejects_reversed_range(tmp_path):
    from archive_magic_fetch.cli import main

    descriptor = write_cli_descriptor(tmp_path)
    code = main(
        [
            str(descriptor),
            "--start",
            "20050101",
            "--end",
            "20040101",
        ]
    )
    assert code == 2


def test_cli_reset_data_flag(tmp_path):
    from archive_magic_fetch.cli import parse_args

    descriptor = write_cli_descriptor(tmp_path)
    args = parse_args([str(descriptor), "--reset-data"])
    assert args.reset_data is True

    args = parse_args([str(descriptor)])
    assert args.reset_data is False


def test_cli_uses_descriptor_configured_history(tmp_path, monkeypatch):
    from archive_magic_fetch import cli

    descriptor = write_cli_descriptor(tmp_path)
    captured = []

    def run(settings):
        captured.append(settings)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(cli, "run_fetch", run)
    assert cli.main([str(descriptor.parent)]) == 0
    assert captured[0].archive_id == "example.org"
    assert captured[0].date_start == "20000101000000"
    assert captured[0].date_end == "20011231235959"
    assert captured[0].storage.workspace_directory == (tmp_path / "workspace").resolve()


def test_remote_reset_rejects_dates_and_warns_before_full_rebuild(
    tmp_path,
    monkeypatch,
    capsys,
):
    from archive_magic_fetch import cli

    descriptor = write_cli_descriptor(tmp_path, authority="remote")
    assert cli.main([str(descriptor), "--reset-data", "--start", "2001"]) == 2
    assert "complete configured date range" in capsys.readouterr().err

    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda settings: SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(descriptor), "--reset-data"]) == 0
    warning = capsys.readouterr().err
    assert "delete and rebuild the entire remote archive prefix" in warning
    assert "playback will be unavailable" in warning
