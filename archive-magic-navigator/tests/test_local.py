from pathlib import Path

import pytest
from archive_magic_navigator.errors import ValidationError

from archive_magic_navigator.local import LocalArchiveStore


def test_local_snapshot_updates_and_adds_year(collection_factory, tmp_path):
    _, root, index, _ = collection_factory()
    store = LocalArchiveStore(root, tmp_path / "cache", 300)
    archive = store.load_archive("example.org")
    original = index.read_bytes()
    before = archive.replay_index.stat()
    store._poll_archive("example.org")
    assert archive.replay_index.stat() == before

    updated = original.replace(b'"offset": "0"', b'"offset": "16"')
    index.write_bytes(updated)
    _, _, added, _ = collection_factory(collection_id="2021")
    store._poll_archive("example.org")

    assert archive.replay_index.read_bytes() == updated + added.read_bytes()
    assert len(store._states["example.org"].collections) == 2
    assert not list((tmp_path / "cache").rglob("*.warc.gz"))
    index.unlink()
    store._poll_archive("example.org")
    assert archive.replay_index.read_bytes() == updated + added.read_bytes()


@pytest.mark.parametrize("failure", ["listing", "copy", "validation", "publication"])
def test_local_failed_refresh_preserves_snapshot_and_retries(
    collection_factory, tmp_path, monkeypatch, failure,
):
    _, root, index, _ = collection_factory()
    store = LocalArchiveStore(root, tmp_path / "cache", 300)
    archive = store.load_archive("example.org")
    old = archive.replay_index.read_bytes()
    old_state = store._states.copy()
    old_signatures = store._signatures.copy()
    updated = old.replace(b'"offset": "0"', b'"offset": "16"')
    index.write_bytes(updated)
    _, _, new_index, _ = collection_factory(collection_id="2021")
    valid_new = new_index.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected failure")

    with monkeypatch.context() as patch:
        if failure == "listing":
            patch.setattr("archive_magic_navigator.local.select_archive_root", fail)
        elif failure == "copy":
            patch.setattr("archive_magic_navigator.local.shutil.copyfile", fail)
        elif failure == "validation":
            new_index.write_text("unfinished index")
        else:
            import os
            real_replace = os.replace

            def fail_publication(source, target):
                if Path(target) == archive.replay_index:
                    fail()
                real_replace(source, target)

            patch.setattr("archive_magic_navigator.store.os.replace", fail_publication)
        with pytest.raises((OSError, ValidationError)):
            store._poll_archive("example.org")

    assert archive.replay_index.read_bytes() == old
    assert store._states == old_state
    assert store._signatures == old_signatures
    assert not list((tmp_path / "cache").rglob(".tmp-*"))
    new_index.write_bytes(valid_new)
    store._poll_archive("example.org")
    assert archive.replay_index.read_bytes() == updated + valid_new


def test_local_rejects_source_changed_during_copy(collection_factory, tmp_path, monkeypatch):
    import shutil

    _, root, index, _ = collection_factory()
    store = LocalArchiveStore(root, tmp_path / "cache", 300)
    archive = store.load_archive("example.org")
    old = archive.replay_index.read_bytes()
    index.write_bytes(old.replace(b'"offset": "0"', b'"offset": "16"'))
    real_copy = shutil.copyfile

    def changing_copy(source, destination):
        real_copy(source, destination)
        source.write_bytes(old)

    monkeypatch.setattr("archive_magic_navigator.local.shutil.copyfile", changing_copy)
    with pytest.raises(ValidationError, match="changed while being copied"):
        store._poll_archive("example.org")
    assert archive.replay_index.read_bytes() == old


def test_merge_orders_years_by_url_and_retains_open_readers(collection_factory, tmp_path):
    def entries(year, keys):
        return [(key, f"{year}0101000000", {
            "filename": f"example.org-{year}-001.warc.gz", "offset": "0", "length": "16",
        }) for key in keys]

    _, root, first, _ = collection_factory(entries=entries("2020", ["a)/", "z)/"]))
    store = LocalArchiveStore(root, tmp_path / "cache", 300)
    archive = store.load_archive("example.org")
    with archive.replay_index.open("rb") as reader:
        collection_factory(collection_id="2021", entries=entries("2021", ["b)/", "y)/"]))
        store._poll_archive("example.org")
        assert reader.read() == first.read_bytes()
    assert [line.split()[0] for line in archive.replay_index.read_bytes().splitlines()] == [
        b"a)/", b"b)/", b"y)/", b"z)/",
    ]


def test_polling_recovers_after_failure(collection_factory, tmp_path, monkeypatch, capsys):
    import threading

    _, root, index, _ = collection_factory()
    store = LocalArchiveStore(root, tmp_path / "cache", 0.01)
    archive = store.load_archive("example.org")
    updated = index.read_bytes().replace(b'"offset": "0"', b'"offset": "16"')
    index.write_bytes(updated)
    recovered = threading.Event()
    attempts = []
    real_poll = store._poll_archive

    def transient_failure(archive_id):
        attempts.append(archive_id)
        if len(attempts) == 1:
            raise OSError("temporarily unavailable")
        real_poll(archive_id)
        recovered.set()

    monkeypatch.setattr(store, "_poll_archive", transient_failure)
    store.start_polling()
    try:
        assert recovered.wait(2)
    finally:
        store.stop_polling()
    assert not store._thread.is_alive()
    assert archive.replay_index.read_bytes() == updated
    assert "temporarily unavailable" in capsys.readouterr().err
