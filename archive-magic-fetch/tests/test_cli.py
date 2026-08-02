from datetime import datetime, timezone

import pytest

from archive_magic_fetch import cli, retry
from archive_magic_fetch.job import FetchRequest
from archive_magic_fetch.retrieval import DEFAULT_CONCURRENCY


def install_job_clock(monkeypatch, *, elapsed_seconds=0):
    started = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    ended = datetime.fromtimestamp(
        started.timestamp() + elapsed_seconds,
        tz=timezone.utc,
    )
    wall_times = iter((started, ended))
    ticks = iter((100.0, 100.0 + elapsed_seconds))
    monkeypatch.setattr(cli, "_utc_now", lambda: next(wall_times))
    monkeypatch.setattr(cli, "_monotonic", lambda: next(ticks))


def test_parse_args_returns_normalized_fetch_request(monkeypatch):
    monkeypatch.setattr(
        cli,
        "current_utc_cdx_timestamp",
        lambda: "20260722123456",
    )

    request = cli.parse_args(["example.com/*"])

    assert request == FetchRequest(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="20260722123456",
        warc_mode="all",
        files_mode="none",
        rewrite_local=False,
        concurrency=DEFAULT_CONCURRENCY,
        retries=retry.DEFAULT_RETRIES,
    )


def test_parse_args_preserves_explicit_dates():
    request = cli.parse_args(
        ["example.com/*", "--start", "2018", "--end", "20200131"]
    )

    assert request.date_start == "2018"
    assert request.date_end == "20200131"


def test_parse_args_accepts_unique_only_for_files():
    assert cli.parse_args(
        ["example.com/*", "--files", "unique"]
    ).files_mode == "unique"
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["example.com/*", "--warc", "unique"])

    assert raised.value.code == 2


def test_parse_args_accepts_supported_concurrency_and_retry_counts():
    assert cli.parse_args(
        ["example.com/*", "--concurrency", "1"]
    ).concurrency == 1
    assert cli.parse_args(
        ["example.com/*", "--retries", "0"]
    ).retries == 0
    assert cli.parse_args(
        ["example.com/*", "--retries", "100"]
    ).retries == 100


@pytest.mark.parametrize(
    ("arguments", "message"),
    (
        (("--retries", "-1"), "--retries: cannot be negative"),
        (("--retries", "1.5"), "--retries"),
        (("--concurrency", "0"), "--concurrency: must be at least 1"),
        (
            ("--rewrite-local", "--files", "none"),
            "--rewrite-local requires --files latest, unique, or all",
        ),
        (
            ("--rewrite-local", "--warc", "none"),
            "--rewrite-local requires --files latest, unique, or all",
        ),
    ),
)
def test_usage_errors_exit_before_job_timing(arguments, message, capsys):
    with pytest.raises(SystemExit) as raised:
        cli.main(["example.com/*", *arguments])

    assert raised.value.code == 2
    output = capsys.readouterr()
    assert message in output.err
    assert "Job started:" not in output.out
    assert "Job ended:" not in output.out


def test_parse_args_rejects_unapproved_arguments():
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["example.com/*", "--output", "elsewhere"])

    assert raised.value.code == 2


def test_main_times_successful_job_and_passes_request(monkeypatch, capsys):
    install_job_clock(monkeypatch, elapsed_seconds=95)
    received = {}

    def succeed(request, *, console_log):
        received["request"] = request
        received["console_log"] = console_log
        return True

    monkeypatch.setattr(cli, "run_fetch", succeed)

    assert cli.main(["example.com/*", "--end", "2020"]) == 0
    assert received["request"].date_end == "2020"
    assert received["console_log"] is not None
    assert capsys.readouterr().out == (
        "Job started: 2026-07-24T12:00:00Z\n"
        "Job ended: 2026-07-24T12:01:35Z\n"
        "Job duration: 1.6 minutes\n"
    )


def test_main_maps_partial_failure_to_one(monkeypatch):
    install_job_clock(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda request, *, console_log: False,
    )

    assert cli.main(["example.com/*"]) == 1


def test_main_reports_fatal_error_and_still_prints_timing(
    monkeypatch,
    capsys,
):
    install_job_clock(monkeypatch, elapsed_seconds=30)

    def fail(*args, **kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(cli, "run_fetch", fail)

    assert cli.main(["example.com/*"]) == 1
    output = capsys.readouterr()
    assert output.err == "ERROR: discovery failed\n"
    assert output.out.endswith(
        "Job ended: 2026-07-24T12:00:30Z\n"
        "Job duration: 0.5 minutes\n"
    )
