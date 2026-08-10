"""Console formatting and playback retry presentation."""

from __future__ import annotations

from unittest.mock import MagicMock

from archive_magic_fetch.fetch import (
    _capture_link,
    _download_with_retries,
    _format_elapsed,
    _style_result,
)
from archive_magic_fetch.models import (
    FailureCategory,
    INVALID_URI_PAYLOAD_DIGEST,
    RunMetrics,
)
from helpers import make_capt, playback

def test_console_link_uses_compact_label_and_full_destination():
    identity = make_capt(
        url="http://www.example.org/a",
        ts="20080516181742",
    )

    plain = _capture_link(identity, enabled=False)
    linked = _capture_link(identity, enabled=True)

    assert plain == "20080516181742/http://example.org/a"
    assert plain in linked
    assert "https://web.archive.org/web/20080516181742/http://www.example.org/a" in linked
    assert linked.startswith("\033]8;;")
    assert _style_result("Error", "error", enabled=False) == "Error"
    assert _style_result("Error", "error", enabled=True) == "\033[1;31mError\033[0m"


def test_serial_retry_uses_five_then_ten_seconds(capsys):
    identity = make_capt()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary")
        return playback(capture)

    result, failure = _download_with_retries(
        MagicMock(),
        identity,
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
        number=2,
        total=1234,
    )

    assert result is not None
    assert failure is None
    assert attempts == 3
    assert sleeps == [5.0, 10.0]
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert all(line.startswith("   2/1234:") for line in lines)
    assert all("https://web.archive.org/web/" not in line for line in lines)
    assert "20040615000000/http://example.org/" in lines[0]


def test_serial_retry_honors_retry_after():
    class TestRateLimitError(Exception):
        retry_after = 17

    identity = make_capt()
    attempts = 0
    sleeps: list[float] = []

    def download(_client, capture):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TestRateLimitError("429")
        return playback(capture)

    result, failure = _download_with_retries(
        MagicMock(),
        identity,
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
    )

    assert result is not None
    assert failure is None
    assert sleeps == [17.0]


def test_permanent_failure_does_not_retry():
    download = MagicMock(side_effect=RuntimeError("permanent"))
    sleeps: list[float] = []

    result, failure = _download_with_retries(
        MagicMock(),
        make_capt(),
        download_fn=download,
        metrics=RunMetrics(),
        sleep=sleeps.append,
    )

    assert result is None
    assert failure is not None
    assert download.call_count == 1
    assert sleeps == []


def test_invalid_uri_digest_skipsplayback():
    download = MagicMock(side_effect=AssertionError("should not download"))

    result, failure = _download_with_retries(
        MagicMock(),
        make_capt(digest=INVALID_URI_PAYLOAD_DIGEST),
        download_fn=download,
        metrics=RunMetrics(),
        sleep=lambda _seconds: None,
    )

    assert result is None
    assert failure is not None
    assert failure.category == FailureCategory.UNAVAILABLE
    download.assert_not_called()


def test_elapsed_format_uses_unbounded_hours():
    assert _format_elapsed(3661.9) == "01:01:01"
    assert _format_elapsed(25 * 60 * 60 + 2) == "25:00:02"


def test_cli_rejects_reversed_range():
    from archive_magic_fetch.cli import main

    code = main(
        [
            "http://example.org/",
            "--start",
            "20050101",
            "--end",
            "20040101",
        ]
    )
    assert code == 2


def test_cli_reset_data_flag():
    from archive_magic_fetch.cli import parse_args

    args = parse_args(["http://example.org/", "--reset-data"])
    assert args.reset_data is True

    args = parse_args(["http://example.org/"])
    assert args.reset_data is False


