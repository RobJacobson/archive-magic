"""Durable URL-group writes, strict tail recovery, and resumed promotion."""

import gzip
import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.archive.inventory_collection import inventory_collection, revisit_from_stored, stored_from_capture
from archive_magic_fetch.archive.layout import ArchiveLayout, list_collection_warcs
from archive_magic_fetch.archive.scan_warcs import scan_warc
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.contracts import FailureAdvice, SourceAdapter
from archive_magic_fetch.models import CaptureListing, CaptureRef, FailureCategory
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from archive_magic_fetch.pipeline.stage_year import YearStage
from archive_magic_fetch.pipeline.write_captures import (
    _CollectionWarcWriter, _build_response_record, _build_revisit_record, _serialize_record, _warcinfo,
)
from helpers import make_capt, make_collection, playback


def fixture_source(captures, calls):
    @contextmanager
    def client(stats):
        yield object()

    def fetch(client, capture):
        calls.append(capture.identity)
        return playback(capture.identity)

    return SourceAdapter(
        lambda request, stats: CaptureListing(tuple(CaptureRef(c, "text/html") for c in captures), {}),
        client, fetch, lambda _: None,
        lambda error, attempt: FailureAdvice(FailureCategory.UNAVAILABLE, False),
        lambda capture: "fixture://" + capture.identity.timestamp,
        name="fixture",
    )


def settings(tmp_path, **changes):
    return FetchSettings("example.org", "20040101000000", "20041231235959", "example.org",
                         FetchOutput("local", tmp_path / "data"), playback_workers=1, **changes)


@pytest.mark.parametrize("failure", [OSError("interrupted writer"), KeyboardInterrupt()])
def test_completed_url_survives_and_is_not_downloaded_again(tmp_path, monkeypatch, failure):
    import archive_magic_fetch.pipeline.write_captures as writing

    first = make_capt(url="http://example.org/a", ts="20040601000000")
    second = make_capt(url="http://example.org/b", ts="20040602000000")
    calls = []
    source, config = fixture_source([first, second], calls), settings(tmp_path)
    with monkeypatch.context() as patch:
        def stop(*args, **kwargs):
            raise failure
        patch.setattr(writing, "log_url_outcome", stop)
        if isinstance(failure, KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                run_fetch(config, source=source)
        else:
            assert run_fetch(config, source=source).failed_years == (2004,)
    stage_path = config.output.data_directory / ".staging" / "2004"
    state = json.loads((stage_path / "work.json").read_text())
    assert len(state["durable_sizes"]) == 1
    assert calls == [first]
    result = run_fetch(config, source=source)
    assert result.exit_code == 0 and result.metrics.local_reuses == 1
    assert calls == [first, second]
    assert len(result.layout.collection_index("2004").read_text().splitlines()) == 2
    assert not stage_path.exists()


def test_index_failure_resumes_with_zero_downloads_and_promotes(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.run_fetch as runner

    first = make_capt(digest=payload_digest(b"hello"))
    later = replace(first, timestamp="20040616000000")
    calls, config = [], settings(tmp_path)
    source = fixture_source([first, later], calls)
    with monkeypatch.context() as patch:
        patch.setattr(runner, "build_collection_index", lambda *a, **kw: (_ for _ in ()).throw(OSError("index failed")))
        assert run_fetch(config, source=source).exit_code == 1
    result = run_fetch(config, source=source)
    assert calls == [first]
    assert result.metrics.downloads == 0 and result.metrics.local_reuses == 2
    assert len(result.layout.collection_index("2004").read_text().splitlines()) == 2


def test_interrupted_group_rotation_recovers_full_response_and_revisits(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.write_captures as writing

    first = make_capt(ts="20040601000000", digest=payload_digest(b"hello"))
    second = replace(first, timestamp="20040602000000", payload_digest=payload_digest(b"other"))
    third = replace(first, timestamp="20040603000000")
    calls, config = [], settings(tmp_path, warc_target_bytes=1)
    source = fixture_source([first, second, third], calls)
    def fetch(client, capture):
        calls.append(capture.identity)
        return playback(capture.identity, body=b"other" if capture.identity == second else b"hello")
    source = replace(source, fetch=fetch)
    append = writing._append_bytes
    with monkeypatch.context() as patch:
        def interrupt(path, data):
            if b"WARC-Date: 2004-06-02" in gzip.decompress(data):
                with path.open("ab") as stream:
                    stream.write(data[:-4])
                raise KeyboardInterrupt()
            append(path, data)
        patch.setattr(writing, "_append_bytes", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run_fetch(config, source=source)
    work = config.output.data_directory / ".staging" / "2004"
    assert json.loads((work / "work.json").read_text())["durable_sizes"] == {}
    result = run_fetch(config, source=source)
    assert result.exit_code == 0
    assert calls == [first, second, second]
    assert result.metrics.local_reuses == 1 and result.metrics.revisits == 1
    entries = [json.loads(line.split(" ", 2)[2])
               for line in result.layout.collection_index("2004").read_text().splitlines()]
    assert len(entries) == 3 and entries[-1]["mime"] == "warc/revisit"
    assert len({entry["filename"] for entry in entries}) == 3


def test_cancellation_drains_inflight_url_and_preserves_completed_group(tmp_path, monkeypatch):
    from threading import Event
    from types import SimpleNamespace
    import archive_magic_fetch.pipeline.write_captures as writing

    first = make_capt(url="http://example.org/a")
    second = make_capt(url="http://example.org/b")
    started, drained = Event(), Event()
    calls = []
    source = fixture_source([first, second], calls)
    @contextmanager
    def client(stats):
        yield SimpleNamespace(stats=stats)
    def fetch(client, capture):
        calls.append(capture.identity)
        if capture.identity == first:
            assert started.wait(2)
        else:
            started.set()
            assert client.stats._local.cancelled.wait(2)
            drained.set()
        return playback(capture.identity)
    with monkeypatch.context() as patch:
        patch.setattr(writing, "log_url_outcome", lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run_fetch(replace(settings(tmp_path), playback_workers=2),
                      source=replace(source, open_client=client, fetch=fetch))
    assert drained.is_set()
    retry_calls = []
    result = run_fetch(settings(tmp_path), source=fixture_source([first, second], retry_calls))
    assert result.exit_code == 0 and retry_calls == [second]


def test_url_sync_precedes_checkpoint_and_logging_across_rotation(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.stage_year as staging
    import archive_magic_fetch.pipeline.write_captures as writing

    capture = make_capt(digest=payload_digest(b"hello"))
    later = replace(capture, timestamp="20040616000000")
    events = []
    real_sync, real_save, real_log = staging.sync_file, staging.write_json_durably, writing.log_url_outcome
    def sync(path):
        events.append(("sync", path.name))
        real_sync(path)
    def save(path, value):
        if path.name == "work.json" and value["durable_sizes"]:
            events.append(("checkpoint", dict(value["durable_sizes"])))
        real_save(path, value)
    def log(*args, **kwargs):
        events.append(("log", None))
        real_log(*args, **kwargs)
    monkeypatch.setattr(staging, "sync_file", sync)
    monkeypatch.setattr(staging, "write_json_durably", save)
    monkeypatch.setattr(writing, "log_url_outcome", log)
    result = run_fetch(settings(tmp_path, warc_target_bytes=1), source=fixture_source([capture, later], []))
    assert result.exit_code == 0
    first_log = next(i for i, e in enumerate(events) if e[0] == "log")
    checkpoint = events[first_log - 1]
    assert checkpoint[0] == "checkpoint" and len(checkpoint[1]) == 2
    synced = {name for kind, name in events[:first_log - 1] if kind == "sync"}
    assert set(checkpoint[1]) <= synced


@pytest.mark.parametrize("boundary", ["sync", "checkpoint"])
def test_checkpoint_failure_retains_complete_records_without_false_completion(tmp_path, monkeypatch, capsys, boundary):
    import archive_magic_fetch.pipeline.stage_year as staging

    capture = make_capt()
    calls, config = [], settings(tmp_path)
    source = fixture_source([capture], calls)
    with monkeypatch.context() as patch:
        if boundary == "sync":
            patch.setattr(staging, "sync_file", lambda _: (_ for _ in ()).throw(OSError("sync failed")))
        else:
            save = staging.YearStage._save
            def fail(stage):
                if stage.state.durable_sizes:
                    raise OSError("checkpoint failed")
                save(stage)
            patch.setattr(staging.YearStage, "_save", fail)
        assert run_fetch(config, source=source).exit_code == 1
    assert "1/1 http://example.org/" not in capsys.readouterr().out
    result = run_fetch(config, source=source)
    assert result.exit_code == 0 and result.metrics.downloads == 0
    assert calls == [capture]


def test_resume_never_recopies_tail_or_trusts_stale_cdxj(tmp_path):
    layout = make_collection(tmp_path / "data")
    canonical = layout.collection_warc_path("2004", 1)
    original = canonical.read_bytes()
    stage = YearStage(layout, "2004")
    stage.prepare_mutable_tail(250_000_000)
    capture = make_capt(ts="20040616000000")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(capture))
    stage.checkpoint(list(writer.touched))
    retained = stage.layout.collection_warc_path("2004", 1).read_bytes()
    stage.layout.collection_index("2004").write_text("stale index ignored")
    stage.abort()
    YearStage.recover(layout)
    resumed = YearStage(layout, "2004")
    resumed.prepare_mutable_tail(250_000_000)
    assert resumed.layout.collection_warc_path("2004", 1).read_bytes() == retained
    assert canonical.read_bytes() == original
    assert resumed.prepare_inventory().contains(capture)


def test_incomplete_uncheckpointed_tail_retains_complete_records_and_repairs_footer(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    stage = YearStage(layout, "2004")
    first, second = make_capt(), make_capt(ts="20040616000000")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(first))
    stage.checkpoint(list(writer.touched))
    path = stage.layout.collection_warc_path("2004", 1)
    prefix = path.read_bytes()
    with path.open("ab") as output:
        output.write(_serialize_record(_build_response_record(playback(second)))[:-4])
    resumed = YearStage(layout, "2004")
    inventory = resumed.prepare_inventory()
    assert inventory.identities == {first} and path.read_bytes() == prefix


@pytest.mark.parametrize("kind", ["checkpointed_eof", "boundary", "crc", "frame", "digest", "earlier_shard", "missing"])
def test_corruption_is_preserved_and_rejected(tmp_path, kind):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    stage = YearStage(layout, "2004")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(make_capt()))
    path = stage.layout.collection_warc_path("2004", 1)
    if kind == "boundary":
        stage.state.durable_sizes[path.name] = path.stat().st_size - 1
        stage._save()
    elif kind in {"checkpointed_eof", "missing"}:
        stage.checkpoint(list(writer.touched))
        if kind == "missing":
            path.unlink()
        else:
            path.write_bytes(path.read_bytes()[:-4])
    elif kind == "earlier_shard":
        path.write_bytes(path.read_bytes()[:-4])
        stage.layout.collection_warc_path("2004", 2).write_bytes(_warcinfo("next"))
    elif kind == "crc":
        data = bytearray(path.read_bytes())
        data[-8] ^= 1
        path.write_bytes(data)
    else:
        raw = gzip.decompress(_serialize_record(_build_response_record(playback(make_capt()))))
        if kind == "frame":
            raw = raw[:-1]
        else:
            raw = raw.replace(b"hello", b"jello")
        path.write_bytes(_warcinfo(path.name) + gzip.compress(raw))
    before = {p: p.read_bytes() for p in list_collection_warcs(stage.layout, "2004")}
    with pytest.raises(ValueError, match="(gzip|WARC|digest|checkpoint)"):
        stage.prepare_inventory()
    assert {p: p.read_bytes() for p in before} == before


def test_revisit_block_digest_is_verified_even_when_gzip_crc_is_valid(tmp_path):
    capture = make_capt(digest=payload_digest(b"hello"))
    result = playback(capture)
    revisit = revisit_from_stored(replace(capture, timestamp="20040616000000"), stored_from_capture(result))
    raw = gzip.decompress(_serialize_record(_build_revisit_record(revisit)))
    raw = raw.replace(b"HTTP/1.1 200 OK", b"HTTP/1.1 201 OK")
    path = tmp_path / "corrupt.warc.gz"
    path.write_bytes(_warcinfo(path.name) + _serialize_record(_build_response_record(result)) + gzip.compress(raw))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="block digest mismatch"):
        scan_warc(path, repair_tail=True)
    assert path.read_bytes() == before


def test_canonical_missing_footer_is_never_repaired(tmp_path):
    layout = make_collection(tmp_path / "data")
    path = layout.collection_warc_path("2004", 1)
    path.write_bytes(path.read_bytes()[:-4])
    before = path.read_bytes()
    stage = YearStage(layout, "2004")
    with pytest.raises(ValueError, match="incomplete gzip"):
        stage.prepare_inventory()
    assert path.read_bytes() == before


def test_recovered_inventory_preserves_payload_reuse_restrictions(tmp_path):
    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    matched = make_capt(digest=payload_digest(b"hello"))
    missing = make_capt(url="http://example.org/missing", digest="-")
    mismatch = make_capt(url="http://example.org/mismatch")
    redirect = make_capt(url="http://example.org/redirect", digest=payload_digest(b""), status="301")
    for response in (playback(matched), playback(missing),
                     replace(playback(mismatch), digest_matched=False), playback(redirect, body=b"", status=301)):
        writer.write_playback(response)
    inventory = stage.prepare_inventory()
    def lookup(capture, timestamp="20040616000000", status=None):
        return inventory.lookup_representative(capture.urlkey, capture.payload_digest,
                                               status or capture.status_token, not_after_timestamp=timestamp)
    assert inventory.identities == {matched, missing, mismatch, redirect}
    assert lookup(matched) is not None and lookup(redirect) is not None
    assert lookup(missing) is None and lookup(mismatch) is None
    assert lookup(matched, timestamp="20040101000000") is None
    assert lookup(redirect, status="302") is None


def test_failed_earlier_capture_remains_retryable_after_later_payload_persists(tmp_path, monkeypatch):
    first = make_capt(ts="20040601000000", digest=payload_digest(b"hello"))
    later = replace(first, timestamp="20040602000000")
    calls, broken = [], [True]
    source = fixture_source([first, later], calls)
    def fetch(client, capture):
        calls.append(capture.identity)
        if broken[0] and capture.identity == first:
            raise OSError("unavailable capture")
        return playback(capture.identity)
    source = replace(source, fetch=fetch)
    with monkeypatch.context() as patch:
        patch.setattr('archive_magic_fetch.pipeline.run_fetch.build_collection_index',
                      lambda *a, **kw: (_ for _ in ()).throw(OSError('index interrupted')))
        assert run_fetch(settings(tmp_path), source=source).exit_code == 1
    broken[0] = False
    result = run_fetch(settings(tmp_path), source=source)
    assert result.exit_code == 0 and calls == [first, later, first]
    assert result.metrics.downloads == 1 and result.metrics.local_reuses == 1
    assert len(result.layout.collection_index('2004').read_text().splitlines()) == 2


def test_legacy_partial_files_survive_startup_cleanup(tmp_path):
    from archive_magic_fetch.archive.layout import cleanup_temps
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    stage = layout.root / ".staging" / "2004"
    stage.mkdir(parents=True)
    partial = stage / "example.org-2004-001.warc.gz.partial"
    partial.write_bytes(b"legacy acquisition")
    cleanup_temps(layout)
    YearStage.recover(layout)
    with pytest.raises(ValueError, match="legacy"):
        YearStage(layout, "2004")
    assert partial.read_bytes() == b"legacy acquisition"


@pytest.mark.parametrize("tail", ["empty", "warcinfo", "incomplete_warcinfo"])
def test_empty_or_warcinfo_only_uncheckpointed_tail_is_removed(tmp_path, tail):
    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    path = stage.layout.collection_warc_path("2004", 1)
    data = _warcinfo(path.name)
    path.write_bytes(b"" if tail == "empty" else data[:-4] if tail == "incomplete_warcinfo" else data)
    assert not stage.prepare_inventory().identities
    assert not path.exists()


def test_incompatible_and_legacy_stages_preserved_until_explicit_reset(tmp_path):
    layout = ArchiveLayout(tmp_path / "data", "example.org")
    stage = YearStage(layout, "2004", binding={"source": "one"})
    before = stage.state_path.read_bytes()
    with pytest.raises(ValueError, match="settings mismatch"):
        YearStage(layout, "2004", binding={"source": "two"})
    assert stage.state_path.read_bytes() == before
    stage.state_path.unlink()
    YearStage.recover(layout)
    assert stage.path.exists()
    with pytest.raises(ValueError, match="legacy"):
        YearStage(layout, "2004")
    replacement = YearStage(layout, "2004", reset=True)
    assert replacement.state.reset
    assert YearStage(layout, "2004").reset


def test_ordinary_run_resumes_interrupted_replacement_mode(tmp_path, monkeypatch):
    layout = make_collection(tmp_path / "data")
    old = make_capt()
    replacement = make_capt(url="http://example.org/replacement", digest=payload_digest(b"hello"))
    calls = []
    source = fixture_source([replacement], calls)
    with monkeypatch.context() as patch:
        patch.setattr('archive_magic_fetch.pipeline.write_captures.log_url_outcome',
                      lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run_fetch(settings(tmp_path, reset_data=True), source=source)
    state = layout.root / '.staging' / '2004' / 'work.json'
    assert json.loads(state.read_text())['reset'] is True
    result = run_fetch(settings(tmp_path), source=source)
    assert result.exit_code == 0 and calls == [replacement]
    assert result.metrics.downloads == 0 and result.metrics.local_reuses == 1
    inventory = inventory_collection(layout, '2004')
    assert inventory.contains(replacement) and not inventory.contains(old)


def test_scanner_spills_large_payload_and_keeps_compact_inventory(tmp_path):
    body = b"0123456789" * 200_000
    capture = make_capt(digest=payload_digest(body))
    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    path = stage.layout.collection_warc_path("2004", 1)
    path.write_bytes(_warcinfo(path.name) + _serialize_record(_build_response_record(playback(capture, body=body))))
    inventory = stage.prepare_inventory()
    assert stage.scans[path.name].captures == 1
    assert inventory.contains(capture)
    assert all(not hasattr(response, "body") for response in inventory.by_url_digest.values())


@pytest.mark.parametrize("index_state", ["stale", "corrupt", "incomplete", "missing"])
def test_startup_rebuilds_canonical_index_once_and_promotes_index_only_corrections(tmp_path, monkeypatch, index_state):
    import archive_magic_fetch.archive.scan_warcs as scanning

    layout = make_collection(tmp_path / "data")
    capture, calls = make_capt(), []
    index, path = layout.collection_index("2004"), layout.collection_warc_path("2004", 1)
    expected = index.read_bytes()
    unchanged = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
    if index_state == "missing":
        index.unlink()
    elif index_state == "incomplete":
        index.write_bytes(expected[:len(expected) // 2])
    else:
        index.write_text("" if index_state == "stale" else "invalid CDXJ metadata\n")
    scans = []
    original = scanning.StrictWarcReader._records
    def counted(reader):
        scans.append(reader.path)
        yield from original(reader)
    monkeypatch.setattr(scanning.StrictWarcReader, "_records", counted)
    result = run_fetch(settings(tmp_path), source=fixture_source([capture], calls))
    assert result.exit_code == 0 and calls == [] and result.metrics.local_reuses == 1
    assert index.read_bytes() == expected
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == unchanged
    assert scans == [layout.root / ".staging" / "2004" / path.name]


def test_resumed_no_append_invocation_decompresses_each_shard_only_at_startup(tmp_path, monkeypatch):
    import archive_magic_fetch.archive.scan_warcs as scanning

    capture = make_capt(digest=payload_digest(b"hello"))
    later = replace(capture, timestamp="20040616000000")
    calls, config = [], settings(tmp_path, warc_target_bytes=1)
    source = fixture_source([capture, later], calls)
    with monkeypatch.context() as patch:
        patch.setattr('archive_magic_fetch.pipeline.run_fetch.build_collection_index',
                      lambda *a, **kw: (_ for _ in ()).throw(OSError("interrupted index")))
        assert run_fetch(config, source=source).exit_code == 1
    scans = []
    original = scanning.StrictWarcReader._records
    def counted(reader):
        scans.append(reader.path.name)
        yield from original(reader)
    monkeypatch.setattr(scanning.StrictWarcReader, "_records", counted)
    result = run_fetch(config, source=source)
    assert result.exit_code == 0 and result.metrics.downloads == 0 and calls == [capture]
    assert scans == ["example.org-2004-001.warc.gz", "example.org-2004-002.warc.gz"]
    # The successful run's descriptions use the verified scan counts.
    records = [json.loads(p.read_text()) for p in result.layout.logs_root.glob("*.json")]
    year = next(r["years"]["2004"] for r in records if "2004" in r["years"])
    assert [w["record_count"] for w in year["warcs"]] == [2, 2]


def test_final_index_reads_only_current_writes_but_promotes_retained_shards(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.run_fetch as runner

    first = make_capt(url="http://example.org/a", digest=payload_digest(b"hello"))
    second = make_capt(url="http://example.org/b", digest=payload_digest(b"hello"))
    calls, config = [], settings(tmp_path, warc_target_bytes=1)
    with monkeypatch.context() as patch:
        patch.setattr(runner, "build_collection_index",
                      lambda *a, **kw: (_ for _ in ()).throw(OSError("index interrupted")))
        assert run_fetch(config, source=fixture_source([first], calls)).exit_code == 1
    inputs = []
    original = runner.build_collection_index
    def final_index(*args, **kwargs):
        inputs.append([p.name for p in kwargs["changed_warcs"]])
        return original(*args, **kwargs)
    monkeypatch.setattr(runner, "build_collection_index", final_index)
    result = run_fetch(config, source=fixture_source([first, second], calls))
    assert result.exit_code == 0 and calls == [first, second]
    assert inputs == [["example.org-2004-002.warc.gz"]]
    assert len(list_collection_warcs(result.layout, "2004")) == 2
    assert inventory_collection(result.layout, "2004").identities == {first, second}


@pytest.mark.parametrize("failure_boundary", ["extraction", "checkpoint", "ranges", "install"])
def test_failed_startup_regeneration_never_uses_old_or_partial_index(tmp_path, monkeypatch, failure_boundary):
    import archive_magic_fetch.pipeline.build_collection_index as indexing

    capture, calls, config = make_capt(), [], settings(tmp_path)
    source = fixture_source([capture], calls)
    with monkeypatch.context() as patch:
        patch.setattr('archive_magic_fetch.pipeline.write_captures.log_url_outcome',
                      lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            run_fetch(config, source=source)
    work = config.output.data_directory / ".staging" / "2004"
    state_path = work / "work.json"
    state = json.loads(state_path.read_text())
    state["durable_sizes"] = {}  # A complete uncheckpointed member to salvage.
    state_path.write_text(json.dumps(state))
    index = work / "example.org-2004-index.cdxj"
    index.write_text("forged index must not supply inventory\n")
    before = {p: p.read_bytes() for p in work.glob("*.warc.gz")}
    discoveries = []
    discover = source.discover
    def counted_discovery(*args):
        discoveries.append(True)
        return discover(*args)
    source = replace(source, discover=counted_discovery)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("startup failed")
        if failure_boundary == "extraction":
            original = indexing._ArchiveMagicCDXJIndexer.process_index_entry
            def partial(self, *args):
                original(self, *args)
                raise OSError("index extraction failed")
            patch.setattr(indexing._ArchiveMagicCDXJIndexer, "process_index_entry", partial)
        elif failure_boundary == "checkpoint":
            patch.setattr(YearStage, "checkpoint", fail)
        elif failure_boundary == "ranges":
            patch.setattr(indexing, "validate_cdxj_against_warcs", fail)
        else:
            patch.setattr(indexing, "publish_file_atomically", fail)
        assert run_fetch(config, source=source).exit_code == 1
    assert calls == [capture] and discoveries == []
    assert {p: p.read_bytes() for p in before} == before
    assert index.read_text() == "forged index must not supply inventory\n"
    assert not list(work.glob("*.cdxj.tmp"))
    result = run_fetch(config, source=source)
    assert result.exit_code == 0 and calls == [capture]
    assert result.metrics.local_reuses == 1


def test_recovered_prefix_sync_and_checkpoint_precede_index_install(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.stage_year as staging
    import archive_magic_fetch.pipeline.build_collection_index as indexing

    stage = YearStage(ArchiveLayout(tmp_path / "data", "example.org"), "2004")
    writer = _CollectionWarcWriter(stage.layout, "2004")
    writer.write_playback(playback(make_capt()))
    events = []
    sync, save, publish = staging.sync_file, stage._save, indexing.publish_file_atomically
    def synced(path):
        events.append("sync")
        sync(path)
    def saved():
        events.append("checkpoint")
        save()
    def installed(*args):
        events.append("index")
        publish(*args)
    monkeypatch.setattr(staging, "sync_file", synced)
    monkeypatch.setattr(stage, "_save", saved)
    monkeypatch.setattr(indexing, "publish_file_atomically", installed)
    assert stage.prepare_inventory().contains(make_capt())
    assert events == ["sync", "checkpoint", "index"]
