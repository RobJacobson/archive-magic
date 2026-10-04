"""Playback workers and CLI argument contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    import archive_magic_fetch.run_application as app
    import archive_magic_fetch.cli as cli

    config = write_cli_config(tmp_path)
    config.write_text(
        config.read_text().replace(
            'data_directory = "data"', f'data_directory = "{data_directory}"'
        )
    )
    captured = []

    def run(settings, **kwargs):
        captured.append(settings)
        return SimpleNamespace(exit_code=0)

    monkeypatch.setattr(app, "run_fetch", run)
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
    import archive_magic_fetch.run_application as app
    import archive_magic_fetch.cli as cli

    config = write_cli_config(tmp_path, output_type="remote")
    assert cli.main([str(config), "--reset-data", "--start", "2001"]) == 2
    assert "complete configured date range" in capsys.readouterr().err

    monkeypatch.setattr(
        app,
        "run_fetch",
        lambda settings, **kwargs: SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(config), "--reset-data"]) == 0
    warning = capsys.readouterr().err
    assert "delete and rebuild managed remote archive data" in warning
    assert "playback will be unavailable" in warning


def test_cli_runtime_flags_override_defaults(tmp_path, monkeypatch):
    import archive_magic_fetch.run_application as app
    import archive_magic_fetch.cli as cli

    config = write_cli_config(tmp_path)
    captured = []
    monkeypatch.setattr(
        app,
        "run_fetch",
        lambda item, **kwargs: captured.append(item) or SimpleNamespace(exit_code=0),
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
    import archive_magic_fetch.run_application as app
    import archive_magic_fetch.cli as cli

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
        app,
        "run_fetch",
        lambda item, **kwargs: captured.append(item) or SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(archive), "--config", str(policy)]) == 0
    assert captured[0].playback_workers == 3
    assert captured[0].playback_starts_per_second == 8.0
    assert captured[0].retries == 6
    captured.clear()
    assert (
        cli.main([str(archive), "--config", str(policy), "--starts-per-second", "1.5"])
        == 0
    )
    assert captured[0].playback_workers == 3
    assert captured[0].playback_starts_per_second == 1.5
    assert captured[0].retries == 6


def test_cli_uses_cdx_settings_from_toml(tmp_path, monkeypatch):
    import archive_magic_fetch.run_application as app
    import archive_magic_fetch.cli as cli

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
        app,
        "run_fetch",
        lambda item, **kwargs: captured.append(item) or SimpleNamespace(exit_code=0),
    )
    assert cli.main([str(path)]) == 0
    assert captured[0].cdx_window_days == 3
    assert captured[0].cdx_page_limit == 2500


def test_cli_rejects_start_before_project_range(tmp_path):
    from archive_magic_fetch.cli import main

    config = write_cli_config(tmp_path)
    assert main([str(config), "--start", "1999-01-01"]) == 2


def test_log_url_outcome_omits_already_represented_lines(capsys):
    from archive_magic_fetch.models import CaptureKind, CaptureOutcome, CaptureRef, UrlOutcome
    from archive_magic_fetch.runtime.report_progress import log_url_outcome

    existing = make_capt(ts="19990117001820")
    downloaded = make_capt(
        ts="19990117071312", digest="sha1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    )
    outcome = UrlOutcome(
        url="http://www.nclr.org/special/award.html",
        captures=(
            CaptureOutcome(capture=CaptureRef(existing, "text/html"), kind=CaptureKind.EXISTING),
            CaptureOutcome(
                capture=CaptureRef(downloaded, "text/html"),
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


@pytest.mark.parametrize("source", [None, "wayback", "common-crawl"])
def test_source_selection_in_application(tmp_path, monkeypatch, source):
    import archive_magic_fetch.run_application as app
    from archive_magic_fetch.cli import main
    from archive_magic_fetch.config.load_archive_config import load_config

    config = write_cli_config(tmp_path)
    if source:
        config.write_text(config.read_text().replace('[archive]', f'[archive]\nsource = "{source}"'))
    assert load_config(config).source == (source or "wayback")
    selected = []
    monkeypatch.setattr(app, "build_wayback_source", lambda **kw: "wayback")
    monkeypatch.setattr(app, "build_common_crawl_source", lambda **kw: "common-crawl")
    monkeypatch.setattr(app, "run_fetch", lambda settings, source: selected.append((settings, source)) or SimpleNamespace(exit_code=0))
    assert main([str(config), "--workers", "2", "--retries", "0"]) == 0
    assert selected[0][1] == (source or "wayback")
    assert selected[0][0].playback_workers == 2 and selected[0][0].retries == 0


def test_invalid_source_and_cc_sync_only(tmp_path, monkeypatch):
    import archive_magic_fetch.run_application as app
    from archive_magic_fetch.cli import main

    config = write_cli_config(tmp_path, output_type="remote")
    config.write_text(config.read_text().replace('[archive]', '[archive]\nsource = "invalid"'))
    assert main([str(config)]) == 2
    config.write_text(config.read_text().replace('source = "invalid"', 'source = "common-crawl"'))
    def forbidden(**kwargs):
        raise AssertionError("sync must not construct any source")
    monkeypatch.setattr(app, "build_wayback_source", forbidden)
    monkeypatch.setattr(app, "build_common_crawl_source", forbidden)
    synced = []
    monkeypatch.setattr(app, "sync_archive", lambda *args: synced.append(args))
    assert main([str(config), "--sync-only"]) == 0 and len(synced) == 1
