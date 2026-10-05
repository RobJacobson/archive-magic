"""Canonical annual staging and ordered rclone publication."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from archive_magic_fetch.archive.inventory_collection import inventory_collection
from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.models import PublicationError
from archive_magic_fetch.pipeline.publication.purge_remote import purge_remote
from archive_magic_fetch.pipeline.publication.sync_archive import sync_archive
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from archive_magic_fetch.runtime.manage_archive_files import archive_lock
from helpers import make_capt, make_collection, make_source, playback


def remote_output(root: Path) -> FetchOutput:
    return FetchOutput(
        "remote",
        root,
        "bucket",
        "example.org",
        "https://s3.example.invalid",
        "auto",
    )


def fake_rclone(tmp_path: Path, monkeypatch, *, fail_call: int = 0) -> Path:
    log = tmp_path / "rclone-args.jsonl"
    script = tmp_path / "rclone"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "p = Path(os.environ['RCLONE_TEST_LOG'])\n"
        "lines = p.read_text().splitlines() if p.exists() else []\n"
        "p.write_text('\\n'.join([*lines, json.dumps(sys.argv[1:])]) + '\\n')\n"
        "if len(lines) + 1 == int(os.environ.get('RCLONE_FAIL_CALL', '0')):\n"
        "    sys.exit(7)\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RCLONE_TEST_LOG", str(log))
    monkeypatch.setenv("RCLONE_FAIL_CALL", str(fail_call))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_fetch_waits_for_sync_and_stops_after_sync_failure(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.run_fetch as fetch_module
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult

    queried = []
    synced = []
    first = make_capt(ts="20040615000000")
    second = make_capt(ts="20050615000000")

    def cdx(*, date_start, **_kwargs):
        year = date_start[:4]
        queried.append(year)
        capture = first if year == "2004" else second
        return _CdxResult(
            captures=(CaptureRef(identity=capture, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        )

    def fail_sync(self, year=None):
        synced.append(year)
        raise PublicationError("simulated upload outage")

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx", cdx
    )
    monkeypatch.setattr(fetch_module.BucketStorage, "publish", fail_sync)
    root = tmp_path / "data"
    settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20051231235959",
        archive_id="example.org",
        output=remote_output(root),
    )
    with pytest.raises(PublicationError, match="upload outage"):
        run_fetch(
            settings,
            sleep=lambda _seconds: None,
            source=make_source(
                settings,
                client_factory=lambda: MagicMock(),
                download=lambda _client, identity: playback(identity),
                sleep=lambda _seconds: None,
            ),
        )
    assert queried == ["2004"]
    assert synced == ["2004"]
    assert inventory_collection(ArchiveLayout(root, "example.org"), "2004").contains(
        first
    )
    assert not (root / ".staging").exists()


def test_explicit_remote_reset_purges_configured_prefix(tmp_path, monkeypatch):
    log = fake_rclone(tmp_path, monkeypatch)
    purge_remote(remote_output(tmp_path / "data"), "example.org")
    invoked = calls(log)
    assert len(invoked) == 2
    assert invoked[1][2:4] == ["delete", "archive:bucket/example.org/data"]
    assert "- **" in invoked[1]
    assert "purge" not in invoked[1]


def test_remote_reset_purges_before_rebuilding_local_archive(tmp_path, monkeypatch):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import (
        _CdxResult,
        load_or_fetch_year_cdx,
    )

    layout = make_collection(tmp_path / "data")
    old = make_capt()
    replacement = make_capt(
        url="http://example.org/reset",
        ts="20040701000000",
        digest="sha1:" + "D" * 32,
    )
    log = fake_rclone(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx",
        lambda **_kwargs: _CdxResult(
            captures=(CaptureRef(identity=replacement, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        ),
    )
    from archive_magic_fetch.pipeline.discovery.cache import wayback_path
    cache = wayback_path(tmp_path / "discovery", "http://example.org/", 2004)
    load_or_fetch_year_cdx(
        index_directory=tmp_path / "discovery",
        year=2004,
        current_year=2005,
        url_pattern="http://example.org/",
    )
    before = cache.read_bytes()
    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx",
        lambda **_kw: pytest.fail("reset re-fetched cached CDX"),
    )
    run_settings = FetchSettings(
        url_pattern="http://example.org/",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        output=remote_output(layout.root),
        reset_data=True,
    )
    result = run_fetch(
        run_settings,
        sleep=lambda _seconds: None,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            download=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        ),
    )

    assert result.exit_code == 0
    assert [args[2] for args in calls(log)] == ["lsf", "delete"]
    inventory = inventory_collection(layout, "2004")
    assert inventory.contains(replacement)
    assert not inventory.contains(old)
    assert cache.read_bytes() == before




@pytest.fixture(autouse=True)
def mock_s3(monkeypatch):
    from bucket_helpers import Bucket
    bucket = Bucket()
    monkeypatch.setattr('archive_magic_fetch.pipeline.publication.storage.boto3.client', lambda *a, **kw: bucket)
    return bucket
