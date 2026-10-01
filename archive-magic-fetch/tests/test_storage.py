"""Canonical annual staging and ordered rclone publication."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from archive_magic_fetch.collection import ArchiveLayout, ensure_collection_dirs
from archive_magic_fetch.config import FetchOutput
from archive_magic_fetch.fetch import FetchSettings, run_fetch
from archive_magic_fetch.index import publish_collection_index
from archive_magic_fetch.inventory import inventory_collection
from archive_magic_fetch.staging import YearStage, recover_stages
from archive_magic_fetch.storage import (
    PublicationError,
    archive_lock,
    purge_remote,
    sync_archive,
)
from archive_magic_fetch.warc import CollectionWarcWriter
from helpers import make_capt, playback


def make_collection(root: Path) -> ArchiveLayout:
    layout = ArchiveLayout(root, "example.org")
    ensure_collection_dirs(layout)
    writer = CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt()))
    writer.close()
    publish_collection_index(layout, "2004")
    return layout


def remote_output(root: Path) -> FetchOutput:
    return FetchOutput(
        "remote", root, "bucket", "example.org",
        "https://s3.example.invalid", "auto",
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
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_rclone_uploads_warcs_then_index_then_prunes_warcs(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    log = fake_rclone(tmp_path, monkeypatch)

    sync_archive(layout, remote_output(layout.root))

    invoked = calls(log)
    assert [args[2] for args in invoked] == ["copy", "sync", "sync"]
    assert all(args[4] == "archive:bucket/example.org" for args in invoked)
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
    assert [args[2] for args in calls(log)[2:]] == ["copy", "sync", "sync"]


def test_staged_append_preserves_prior_offsets_and_promotes_index_last(tmp_path):
    layout = make_collection(tmp_path / "data")
    warc = layout.collection_warc_path("2004", 1)
    old_bytes = warc.read_bytes()
    old_index = layout.collection_index("2004").read_bytes()
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    second = make_capt(
        url="http://example.org/new", ts="20040616000000",
        digest="sha1:" + "B" * 32,
    )
    writer = CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(second))
    changed = writer.close()
    publish_collection_index(
        stage.layout, "2004", changed_warcs=[item.path for item in changed],
    )

    assert warc.read_bytes() == old_bytes
    assert layout.collection_index("2004").read_bytes() == old_index
    stage.commit(changed, index_changed=True)

    assert warc.read_bytes().startswith(old_bytes)
    assert inventory_collection(layout, "2004").contains(second)
    assert not (layout.root / ".staging").exists()


def test_interrupted_stage_keeps_canonical_year_unchanged(tmp_path):
    layout = make_collection(tmp_path / "data")
    old_warc = layout.collection_warc_path("2004", 1).read_bytes()
    old_index = layout.collection_index("2004").read_bytes()
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    writer = CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(make_capt(ts="20040616000000")))
    writer.close()
    stage.abort()

    assert layout.collection_warc_path("2004", 1).read_bytes() == old_warc
    assert layout.collection_index("2004").read_bytes() == old_index
    assert not (layout.root / ".staging").exists()


def test_recovery_finishes_validated_promotion_before_sync(tmp_path, monkeypatch):
    import archive_magic_fetch.staging as staging

    layout = make_collection(tmp_path / "data")
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    second = make_capt(
        url="http://example.org/new", ts="20040616000000",
        digest="sha1:" + "B" * 32,
    )
    writer = CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(second))
    changed = writer.close()
    publish_collection_index(
        stage.layout, "2004", changed_warcs=[item.path for item in changed],
    )
    original_replace = staging.os.replace
    failed = False

    def fail_index_once(source, destination):
        nonlocal failed
        if str(destination).endswith("-index.cdxj") and not failed:
            failed = True
            raise OSError("simulated crash during promotion")
        return original_replace(source, destination)

    monkeypatch.setattr(staging.os, "replace", fail_index_once)
    with pytest.raises(OSError, match="simulated crash"):
        stage.commit(changed, index_changed=True)
    monkeypatch.setattr(staging.os, "replace", original_replace)

    recover_stages(layout)
    assert inventory_collection(layout, "2004").contains(second)
    assert not (layout.root / ".staging").exists()


def test_staged_reset_replaces_the_entire_year(tmp_path):
    layout = make_collection(tmp_path / "data")
    old = make_capt()
    replacement = make_capt(
        url="http://example.org/replacement",
        ts="20040701000000",
        digest="sha1:" + "C" * 32,
    )
    stage = YearStage(layout, "2004", reset=True)
    writer = CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(replacement))
    changed = writer.close()
    publish_collection_index(stage.layout, "2004")
    stage.commit(changed, index_changed=True)

    inventory = inventory_collection(layout, "2004")
    assert inventory.contains(replacement)
    assert not inventory.contains(old)


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
    from archive_magic_fetch.cdx import CdxResult
    from archive_magic_fetch.models import ParsedCapture
    import archive_magic_fetch.fetch as fetch_module

    queried = []
    synced = []
    first = make_capt(ts="20040615000000")
    second = make_capt(ts="20050615000000")

    def cdx(*, date_start, **_kwargs):
        year = date_start[:4]
        queried.append(year)
        capture = first if year == "2004" else second
        return CdxResult(
            captures=(ParsedCapture(identity=capture, mime="text/html"),),
            search_url="http://example.org/", match_type=None,
        )

    def fail_sync(_layout, _output, *, year):
        synced.append(year)
        raise PublicationError("simulated upload outage")

    monkeypatch.setattr(fetch_module, "fetch_cdx", cdx)
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
            settings, client_factory=lambda: MagicMock(),
            download_fn=lambda _client, identity: playback(identity),
            sleep=lambda _seconds: None,
        )
    assert queried == ["2004"]
    assert synced == ["2004"]
    assert inventory_collection(ArchiveLayout(root, "example.org"), "2004").contains(first)
    assert not (root / ".staging").exists()


def test_manual_sync_does_not_contact_wayback(tmp_path, monkeypatch):
    from archive_magic_fetch import cli

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
        cli, "run_fetch",
        lambda _settings: pytest.fail("manual sync invoked the fetch pipeline"),
    )

    assert cli.main([str(config), "--sync-only"]) == 0
    assert [args[2] for args in calls(log)] == ["copy", "sync", "sync"]
    assert layout.collection_index("2004").exists()


def test_explicit_remote_reset_purges_configured_prefix(tmp_path, monkeypatch):
    log = fake_rclone(tmp_path, monkeypatch)
    purge_remote(remote_output(tmp_path / "data"))
    invoked = calls(log)
    assert len(invoked) == 1
    assert invoked[0][2:] == ["purge", "archive:bucket/example.org"]


def test_remote_reset_purges_before_rebuilding_local_archive(tmp_path, monkeypatch):
    from archive_magic_fetch.cdx import CdxResult
    from archive_magic_fetch.models import ParsedCapture
    import archive_magic_fetch.fetch as fetch_module

    layout = make_collection(tmp_path / "data")
    old = make_capt()
    replacement = make_capt(
        url="http://example.org/reset",
        ts="20040701000000",
        digest="sha1:" + "D" * 32,
    )
    log = fake_rclone(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fetch_module, "fetch_cdx",
        lambda **_kwargs: CdxResult(
            captures=(ParsedCapture(identity=replacement, mime="text/html"),),
            search_url="http://example.org/", match_type=None,
        ),
    )
    result = run_fetch(
        FetchSettings(
            url_pattern="http://example.org/",
            date_start="20040101000000",
            date_end="20041231235959",
            archive_id="example.org",
            output=remote_output(layout.root),
            reset_data=True,
        ),
        client_factory=lambda: MagicMock(),
        download_fn=lambda _client, identity: playback(identity),
        sleep=lambda _seconds: None,
    )

    assert result.exit_code == 0
    assert [args[2] for args in calls(log)] == ["purge", "copy", "sync", "sync"]
    inventory = inventory_collection(layout, "2004")
    assert inventory.contains(replacement)
    assert not inventory.contains(old)
