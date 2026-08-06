import pytest

from archive_magic_fetch import cli
from archive_magic_fetch.downloads import DEFAULT_WORKER_COUNT
from archive_magic_fetch.fetch import FetchSettings


def test_parse_args_returns_plain_fetch_settings(monkeypatch):
    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260803010203")

    assert cli.parse_args(["example.com/*"]) == FetchSettings(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="20260803010203",
        build_warc=True,
        files_mode="none",
        rewrite_local=False,
        worker_count=DEFAULT_WORKER_COUNT,
        retries=8,
    )


def test_parse_args_accepts_workers_and_output_modes():
    settings = cli.parse_args(
        [
            "example.com/*",
            "--start",
            "2000",
            "--end",
            "2001",
            "--build-warc",
            "false",
            "--files",
            "unique",
            "--workers",
            "3",
            "--retries",
            "0",
        ]
    )

    assert settings.date_start == "2000"
    assert settings.date_end == "2001"
    assert settings.build_warc is False
    assert settings.files_mode == "unique"
    assert settings.worker_count == 3
    assert settings.retries == 0


def test_concurrency_option_is_rejected(capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["example.com/*", "--concurrency", "2"])
    assert "unrecognized arguments: --concurrency 2" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ("--warc", "all"),
        ("--fresh",),
        ("--redirect-capture", "none"),
        ("--build-warc", "latest"),
        ("--build-warc", "True"),
    ],
)
def test_removed_and_invalid_options_are_rejected(arguments):
    with pytest.raises(SystemExit):
        cli.parse_args(["example.com/*", *arguments])


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (("--workers", "0"), "--workers: must be at least 1"),
        (("--retries", "-1"), "--retries: cannot be negative"),
        (
            ("--rewrite-local",),
            "--rewrite-local requires --files latest, unique, or all",
        ),
    ],
)
def test_usage_errors(arguments, message, capsys):
    with pytest.raises(SystemExit):
        cli.parse_args(["example.com/*", *arguments])
    assert message in capsys.readouterr().err


def test_help_describes_worker_responsibility(capsys):
    with pytest.raises(SystemExit) as result:
        cli.parse_args(["--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "--workers N" in output
    assert "Maximum simultaneous WARC builds" in output


def test_main_maps_fetch_result_to_exit_status(monkeypatch):
    seen = []
    monkeypatch.setattr(
        cli,
        "run_fetch",
        lambda settings, **kwargs: seen.append(settings) or False,
    )

    assert cli.main(["example.com/*", "--workers", "2"]) == 1
    assert seen[0].worker_count == 2


def test_main_prints_fatal_error_without_job_chatter(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise RuntimeError("fatal")

    monkeypatch.setattr(cli, "run_fetch", fail)

    assert cli.main(["example.com/*"]) == 1
    output = capsys.readouterr()
    assert output.err == "ERROR: fatal\n"
    assert "Job started" not in output.out
    assert "Job ended" not in output.out
