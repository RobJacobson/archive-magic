import json
import gzip
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

import pytest
from requests import Request
from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter
from wayback import CdxRecord, WaybackSession

from archive_magic_fetch import paths, provenance, replay


def collection(tmp_path):
    return paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )


def create_warc(
    path: Path,
    *,
    target="https://played.example/posts/",
    first_date="2020-01-02T03:04:05Z",
    second_date="2020-01-03T03:04:05Z",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        response = writer.create_warc_record(
            target,
            "response",
            payload=BytesIO(b"hello"),
            http_headers=StatusAndHeaders(
                "200 OK",
                [("Content-Type", "text/plain")],
                protocol="HTTP/1.1",
            ),
            warc_headers_dict={"WARC-Date": first_date},
        )
        writer.write_record(response)
        digest = response.rec_headers.get_header("WARC-Payload-Digest")
        second = writer.create_warc_record(
            target,
            "response",
            payload=BytesIO(b"hello"),
            http_headers=StatusAndHeaders(
                "200 OK",
                [("Content-Type", "text/plain")],
                protocol="HTTP/1.1",
            ),
            warc_headers_dict={"WARC-Date": second_date},
        )
        writer.write_record(second)
    return digest


def read_index(path):
    entries = []
    for line in path.read_text().splitlines():
        urlkey, timestamp, payload = line.split(" ", 2)
        entries.append((urlkey, timestamp, json.loads(payload)))
    return entries


def test_replay_index_uses_warc_identity_and_real_record_ranges(tmp_path):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "posts" / "index.warc.gz"
    digest = create_warc(warc)

    result = replay.generate_replay_index([warc], layout=selected_layout)

    assert result == selected_layout.replay_index
    entries = read_index(result)
    assert [(key, timestamp) for key, timestamp, _ in entries] == [
        ("example,played)/posts", "20200102030405"),
        ("example,played)/posts", "20200103030405"),
    ]
    response = entries[0][2]
    second = entries[1][2]
    assert response == {
        "url": "https://played.example/posts/",
        "mime": "text/plain",
        "status": "200",
        "digest": digest,
        "length": response["length"],
        "offset": response["offset"],
        "filename": "archive/posts/index.warc.gz",
    }
    assert second == {
        "url": "https://played.example/posts/",
        "mime": "text/plain",
        "status": "200",
        "digest": digest,
        "length": second["length"],
        "offset": second["offset"],
        "filename": "archive/posts/index.warc.gz",
    }

    for _, _, entry in entries:
        with warc.open("rb") as stream:
            stream.seek(int(entry["offset"]))
            member = stream.read(int(entry["length"]))
        assert member.startswith(b"\x1f\x8b")
        records = list(ArchiveIterator(BytesIO(member)))
        assert [record.rec_type for record in records] == ["response"]


def test_nested_index_warcs_keep_distinct_collection_relative_names(tmp_path):
    selected_layout = collection(tmp_path)
    root_warc = selected_layout.archive_root / "index.warc.gz"
    nested_warc = selected_layout.archive_root / "posts" / "index.warc.gz"
    create_warc(
        root_warc,
        target="https://example.com/",
        first_date="2020-01-01T00:00:00Z",
        second_date="2020-01-02T00:00:00Z",
    )
    create_warc(nested_warc)

    result = replay.generate_replay_index(
        [nested_warc, root_warc],
        layout=selected_layout,
    )

    filenames = {entry["filename"] for _, _, entry in read_index(result)}
    assert filenames == {
        "archive/index.warc.gz",
        "archive/posts/index.warc.gz",
    }


def test_shared_warc_entries_have_distinct_offsets(tmp_path):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "index.warc.gz"
    create_warc(warc)

    result = replay.generate_replay_index([warc], layout=selected_layout)
    entries = read_index(result)

    assert {entry["filename"] for _, _, entry in entries} == {
        "archive/index.warc.gz"
    }
    assert len({entry["offset"] for _, _, entry in entries}) == 2


def test_replay_index_includes_revisit_records(tmp_path):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "revisits.warc.gz"
    warc.parent.mkdir(parents=True)
    target = "https://example.com/document.pdf"
    with warc.open("xb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        response = writer.create_warc_record(
            target,
            "response",
            payload=BytesIO(b"same"),
            http_headers=StatusAndHeaders(
                "200 OK",
                [("Content-Type", "application/pdf")],
                protocol="HTTP/1.1",
            ),
            warc_headers_dict={"WARC-Date": "2020-01-01T00:00:00Z"},
        )
        writer.write_record(response)
        revisit = writer.create_revisit_record(
            target,
            response.rec_headers.get_header("WARC-Payload-Digest"),
            target,
            "2020-01-01T00:00:00Z",
            http_headers=StatusAndHeaders(
                "200 OK",
                [("Content-Type", "application/pdf")],
                protocol="HTTP/1.1",
            ),
            warc_headers_dict={"WARC-Date": "2020-01-02T00:00:00Z"},
        )
        writer.write_record(revisit)

    result = replay.generate_replay_index([warc], layout=selected_layout)
    entries = read_index(result)

    assert [timestamp for _, timestamp, _ in entries] == [
        "20200101000000",
        "20200102000000",
    ]
    assert entries[0][2]["mime"] == "application/pdf"
    assert entries[1][2]["mime"] == "warc/revisit"
    assert entries[0][2]["digest"] == entries[1][2]["digest"]


def test_replay_publication_replaces_existing_index(tmp_path):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "index.warc.gz"
    create_warc(warc)
    selected_layout.replay_index.parent.mkdir(parents=True)
    selected_layout.replay_index.write_text("old index\n")

    result = replay.generate_replay_index([warc], layout=selected_layout)

    assert result == selected_layout.replay_index
    assert selected_layout.replay_index.read_text() != "old index\n"
    assert not list(selected_layout.replay_index.parent.glob(".index-*"))


def test_failed_indexing_leaves_no_final_or_temporary_index(
    tmp_path,
    monkeypatch,
):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "index.warc.gz"
    create_warc(warc)

    class FailingIndexer:
        def __init__(self, **kwargs):
            pass

        def process_all(self):
            raise RuntimeError("index failed")

    monkeypatch.setattr(replay, "CDXJIndexer", FailingIndexer)

    with pytest.raises(RuntimeError, match="index failed"):
        replay.generate_replay_index([warc], layout=selected_layout)
    assert not selected_layout.replay_index.exists()
    assert not list(selected_layout.replay_index.parent.glob(".index-*"))


def test_failed_reindex_keeps_existing_index(tmp_path, monkeypatch):
    selected_layout = collection(tmp_path)
    warc = selected_layout.archive_root / "index.warc.gz"
    create_warc(warc)
    selected_layout.replay_index.parent.mkdir(parents=True)
    selected_layout.replay_index.write_text("previous valid index\n")

    class FailingIndexer:
        def __init__(self, **kwargs):
            pass

        def process_all(self):
            raise RuntimeError("index failed")

    monkeypatch.setattr(replay, "CDXJIndexer", FailingIndexer)

    with pytest.raises(RuntimeError, match="index failed"):
        replay.generate_replay_index([warc], layout=selected_layout)
    assert (
        selected_layout.replay_index.read_text()
        == "previous valid index\n"
    )
    assert not list(selected_layout.replay_index.parent.glob(".index-*"))


def test_no_warcs_create_no_replay_directory(tmp_path):
    selected_layout = collection(tmp_path)

    assert replay.generate_replay_index([], layout=selected_layout) is None
    assert not selected_layout.replay_index.parent.exists()


def test_wayback_requests_prepares_internationalized_hostname_with_locked_idna():
    session = WaybackSession(user_agent="archive-magic-fetch-test")
    try:
        prepared = session.prepare_request(
            Request("GET", "https://münich.example/archive")
        )
    finally:
        session.close()

    assert prepared.url == "https://xn--mnich-kva.example/archive"
    assert (
        paths.normalize_collection_name("https://münich.example/archive")
        == "xn--mnich-kva.example"
    )


def test_complete_fixture_collection_is_self_consistent(tmp_path):
    selected_layout = collection(tmp_path)
    capture = CdxRecord(
        urlkey="com,example)/",
        timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
        original="https://example.com/",
        mimetype="text/html",
        statuscode=200,
        digest="A" * 32,
        length=100,
    )
    acquisition = provenance.save_acquisition(
        [capture],
        layout=selected_layout,
        url_pattern="https://example.com/*",
        date_start="1995",
        date_end="2020",
        acquired_at=datetime(
            2026,
            7,
            23,
            18,
            45,
            1,
            123456,
            tzinfo=timezone.utc,
        ),
    )
    warc = selected_layout.archive_root / "index.warc.gz"
    create_warc(warc, target="https://example.com/")
    index = replay.generate_replay_index([warc], layout=selected_layout)

    assert {
        path.relative_to(selected_layout.collection_root).as_posix()
        for path in selected_layout.collection_root.rglob("*")
        if path.is_file()
    } == {
        "sources/20260723T184501.123456Z/captures.cdx.gz",
        "sources/20260723T184501.123456Z/query.json",
        "archive/index.warc.gz",
        "replay/index.cdxj",
    }
    with gzip.open(acquisition.captures_path, "rt", encoding="utf-8") as stream:
        assert stream.readline().rstrip() == provenance.CDX_HEADER
    with warc.open("rb") as stream:
        assert [record.rec_type for record in ArchiveIterator(stream)] == [
            "response",
            "response",
        ]
    assert all(
        selected_layout.collection_root.joinpath(entry["filename"]).exists()
        for _, _, entry in read_index(index)
    )
