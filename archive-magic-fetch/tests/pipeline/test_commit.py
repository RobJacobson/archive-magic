"""Behavioral coverage for pipeline / test_commit."""

from __future__ import annotations

import pytest
from archive_magic_fetch.archive.inventory import inventory_collection
from archive_magic_fetch.pipeline.commit import YearStage
from archive_magic_fetch.pipeline.indexing import publish_collection_index
from archive_magic_fetch.pipeline.writing import _CollectionWarcWriter
from helpers import make_capt, make_collection, playback


def test_staged_append_preserves_prior_offsets_and_promotes_index_last(tmp_path):
    layout = make_collection(tmp_path / "data")
    warc = layout.collection_warc_path("2004", 1)
    old_bytes = warc.read_bytes()
    old_index = layout.collection_index("2004").read_bytes()
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    second = make_capt(
        url="http://example.org/new",
        ts="20040616000000",
        digest="sha1:" + "B" * 32,
    )
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(second))
    changed = writer.close()
    publish_collection_index(
        stage.layout,
        "2004",
        changed_warcs=[item.path for item in changed],
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
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(make_capt(ts="20040616000000")))
    writer.close()
    stage.abort()

    assert layout.collection_warc_path("2004", 1).read_bytes() == old_warc
    assert layout.collection_index("2004").read_bytes() == old_index
    assert not (layout.root / ".staging").exists()


def test_recovery_finishes_validated_promotion_before_sync(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.commit as staging

    layout = make_collection(tmp_path / "data")
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    second = make_capt(
        url="http://example.org/new",
        ts="20040616000000",
        digest="sha1:" + "B" * 32,
    )
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(second))
    changed = writer.close()
    publish_collection_index(
        stage.layout,
        "2004",
        changed_warcs=[item.path for item in changed],
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

    YearStage.recover(layout)
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
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(replacement))
    changed = writer.close()
    publish_collection_index(stage.layout, "2004")
    stage.commit(changed, index_changed=True)

    inventory = inventory_collection(layout, "2004")
    assert inventory.contains(replacement)
    assert not inventory.contains(old)
