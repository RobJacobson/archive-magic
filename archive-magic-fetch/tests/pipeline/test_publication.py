"""Canonical annual staging and ordered rclone publication."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from archive_magic_fetch.archive.inventory import inventory_collection
from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.settings import FetchSettings
from archive_magic_fetch.models import PublicationError
from archive_magic_fetch.pipeline.publication.reset import purge_remote
from archive_magic_fetch.pipeline.publication.stage import sync_archive
from archive_magic_fetch.pipeline.runner import run_fetch
from archive_magic_fetch.runtime.filesystem import archive_lock
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


def test_rclone_uploads_warcs_then_index_then_prunes_warcs(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    log = fake_rclone(tmp_path, monkeypatch)

    sync_archive(layout, remote_output(layout.root))

    invoked = calls(log)
    assert [args[2] for args in invoked] == ["lsf", "copy", "sync", "sync"]
    invoked = invoked[1:]
    assert all(args[4] == "archive:bucket/example.org/data" for args in invoked)
    assert ".warc.gz" in " ".join(invoked[0])
    assert "-index.cdxj" in " ".join(invoked[1])
    assert ".warc.gz" in " ".join(invoked[2])
    assert all(".staging" not in " ".join(args) for args in invoked)


def test_rclone_failure_stops_before_deletion_and_is_retryable(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    log = fake_rclone(tmp_path, monkeypatch, fail_call=2)
    before = layout.collection_index("2004").read_bytes()

    with pytest.raises(PublicationError, match="exit code 7"):
        sync_archive(layout, remote_output(layout.root))
    assert len(calls(log)) == 2
    assert layout.collection_index("2004").read_bytes() == before

    monkeypatch.setenv("RCLONE_FAIL_CALL", "0")
    sync_archive(layout, remote_output(layout.root))
    assert [args[2] for args in calls(log)[2:]] == ["lsf", "copy", "sync", "sync"]


def test_missing_aws_credentials_fail_before_rclone(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    log = fake_rclone(tmp_path, monkeypatch)
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.publication.client.Path.home",
        lambda: tmp_path / "no-home",
    )
    with pytest.raises(PublicationError, match="AWS_ACCESS_KEY_ID"):
        sync_archive(layout, remote_output(layout.root))
    assert not log.exists()


def test_empty_archive_refuses_sync_and_lock_prevents_concurrent_sync(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    with pytest.raises(PublicationError, match="does not exist"):
        sync_archive(layout, remote_output(layout.root))
    with archive_lock(layout):
        with pytest.raises(PublicationError, match="another fetch"):
            with archive_lock(layout):
                pass


def test_unindexed_local_warc_refuses_mirror_deletion(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    extra = layout.collection_warc_path("2004", 2)
    extra.write_bytes(b"unfinished")
    log = fake_rclone(tmp_path, monkeypatch)
    with pytest.raises(PublicationError, match="no CDXJ entries"):
        sync_archive(layout, remote_output(layout.root))
    assert not log.exists()


def test_fetch_waits_for_sync_and_stops_after_sync_failure(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.runner as fetch_module
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.wayback import _CdxResult

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

    def fail_sync(_layout, _output, *, year):
        synced.append(year)
        raise PublicationError("simulated upload outage")

    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.wayback._fetch_cdx", cdx
    )
    monkeypatch.setattr(fetch_module, "sync_archive", fail_sync)
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


def test_manual_sync_does_not_contact_wayback(tmp_path, monkeypatch):
    import archive_magic_fetch.app as app
    import archive_magic_fetch.cli as cli

    layout = make_collection(tmp_path / "data")
    log = fake_rclone(tmp_path, monkeypatch)
    config = tmp_path / "fetch.toml"
    config.write_text(
        "[archive]\nid = 'example.org'\nurl_pattern = 'example.org'\n"
        "[output]\ntype = 'remote'\ndata_directory = 'data'\n"
        "bucket = 'bucket'\nprefix = 'example.org'\n"
        "endpoint_url = 'https://s3.example.invalid'\nregion = 'auto'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app,
        "run_fetch",
        lambda _settings: pytest.fail("manual sync invoked the fetch pipeline"),
    )

    assert cli.main([str(config), "--sync-only"]) == 0
    assert [args[2] for args in calls(log)] == ["lsf", "copy", "sync", "sync"]
    assert layout.collection_index("2004").exists()


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
    from archive_magic_fetch.pipeline.discovery.wayback import (
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
        "archive_magic_fetch.pipeline.discovery.wayback._fetch_cdx",
        lambda **_kwargs: _CdxResult(
            captures=(CaptureRef(identity=replacement, mime="text/html"),),
            search_url="http://example.org/",
            match_type=None,
        ),
    )
    cache = tmp_path / "index" / "2004.cdx.json"
    load_or_fetch_year_cdx(
        index_directory=cache.parent,
        year=2004,
        current_year=2005,
        url_pattern="http://example.org/",
    )
    before = cache.read_bytes()
    monkeypatch.setattr(
        "archive_magic_fetch.pipeline.discovery.wayback._fetch_cdx",
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
    assert [args[2] for args in calls(log)] == [
        "lsf",
        "delete",
        "lsf",
        "copy",
        "sync",
        "sync",
    ]
    inventory = inventory_collection(layout, "2004")
    assert inventory.contains(replacement)
    assert not inventory.contains(old)
    assert cache.read_bytes() == before


def test_real_rclone_reset_and_sync_preserve_non_archive_content(tmp_path, monkeypatch):
    """Exercise real rclone filter semantics against disposable local directories."""
    import shutil

    import archive_magic_fetch.pipeline.publication.client as storage

    if shutil.which("rclone") is None:
        pytest.skip("rclone not installed")
    layout = make_collection(tmp_path / "local" / "data")
    cache = layout.root.parent / "index" / "2004.cdx.json"
    cache.parent.mkdir()
    cache.write_text("[]")
    remote = tmp_path / "bucket"
    (remote / "data").mkdir(parents=True)
    (remote / "assets").mkdir()
    protected = {
        "archive.json": b'{"name":"kept"}',
        "assets/logo.png": b"logo",
        "data/other-2004-001.warc.gz": b"other site",
        "data/notes.txt": b"notes",
    }
    for name, data in protected.items():
        (remote / name).write_bytes(data)
    monkeypatch.setattr(
        storage,
        "remote_path",
        lambda output, *, data=True: str(remote / "data" if data else remote),
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    output = remote_output(layout.root)
    sync_archive(layout, output)
    assert (remote / "data" / layout.collection_index("2004").name).exists()
    assert not list(remote.rglob("*.cdx.json"))
    purge_remote(output, layout.archive_id)
    assert not list((remote / "data").glob("example.org-*"))
    assert all((remote / name).read_bytes() == body for name, body in protected.items())
    assert layout.collection_index("2004").exists()
    assert cache.read_text() == "[]"


def test_legacy_flat_objects_stop_sync_and_reset_before_mutation(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.publication.client as storage

    layout = make_collection(tmp_path / "data")
    calls = []

    def run(config, *args):
        calls.append(args)
        return "example.org-2004-001.warc.gz\n"

    monkeypatch.setattr(storage, "run_rclone", run)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    for action in (
        lambda: sync_archive(layout, remote_output(layout.root)),
        lambda: purge_remote(remote_output(layout.root), layout.archive_id),
    ):
        with pytest.raises(PublicationError, match="legacy flat"):
            action()
    assert [args[0] for args in calls] == ["lsf", "lsf"]
