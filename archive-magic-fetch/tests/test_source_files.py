import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from wayback import CdxRecord

from archive_magic_fetch import collection_paths
from archive_magic_fetch import source_files
from archive_magic_fetch.atomic_files import publish_directory_noreplace


ACQUIRED_AT = datetime(
    2026,
    7,
    23,
    18,
    45,
    1,
    123456,
    tzinfo=timezone.utc,
)


def record(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    captured="20200102030405",
    mimetype="text/html",
    statuscode=200,
    digest="A" * 32,
    length=100,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=datetime.strptime(captured, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        original=original,
        mimetype=mimetype,
        statuscode=statuscode,
        digest=digest,
        length=length,
    )


def collection(tmp_path):
    return collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )


def save(tmp_path, captures, **overrides):
    arguments = {
        "layout": collection(tmp_path),
        "url_pattern": "https://example.com/*",
        "date_start": "1995",
        "date_end": "20260723184501",
        "acquired_at": ACQUIRED_AT,
    }
    arguments.update(overrides)
    return source_files.save_search_results(captures, **arguments)


def test_source_snapshot_preserves_order_duplicates_redirects_and_absent_values(
    tmp_path,
):
    first = record(original="https://example.com/café")
    redirect = record(
        urlkey="com,example)/old",
        original="https://example.com/old",
        captured="20210102030405",
        statuscode=301,
    )
    absent = record(
        urlkey="com,example)/missing",
        original="https://example.com/missing",
        captured="20220102030405",
        mimetype="-",
        statuscode=None,
        digest="-",
        length=None,
    )

    result = save(tmp_path, [first, first, redirect, absent])

    with gzip.open(result.captures_path, "rt", encoding="utf-8") as stream:
        lines = stream.read().splitlines()
    assert lines == [
        source_files.CDX_HEADER,
        (
            "com,example)/ 20200102030405 https://example.com/café "
            f"text/html 200 {'A' * 32} 100"
        ),
        (
            "com,example)/ 20200102030405 https://example.com/café "
            f"text/html 200 {'A' * 32} 100"
        ),
        (
            "com,example)/old 20210102030405 https://example.com/old "
            f"text/html 301 {'A' * 32} 100"
        ),
        (
            "com,example)/missing 20220102030405 "
            "https://example.com/missing - - - -"
        ),
    ]

    manifest = json.loads(result.query_path.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["source"] == "internet-archive-wayback-machine"
    assert manifest["url_pattern"] == "https://example.com/*"
    assert manifest["date_start"] == "1995"
    assert manifest["date_end"] == "20260723184501"
    assert manifest["acquired_at"] == "2026-07-23T18:45:01.123456Z"
    assert manifest["archive_magic_fetch_version"] == "0.1.0"
    assert manifest["wayback_version"] == "0.5.1"
    assert manifest["cdx"]["fields"] == list(source_files.CDX_FIELDS)
    assert manifest["cdx"]["record_count"] == 4
    assert manifest["cdx"]["sha256"] == hashlib.sha256(
        result.captures_path.read_bytes()
    ).hexdigest()


def test_source_snapshot_rejects_ambiguous_cdx_tokens(tmp_path):
    invalid = record(original="https://example.com/a path")

    with pytest.raises(ValueError, match="whitespace"):
        save(tmp_path, [invalid])

    assert list(collection(tmp_path).sources_root.iterdir()) == []


def test_search_files_identifier_collision_uses_numeric_suffix(tmp_path):
    first = save(tmp_path, [record()])
    second = save(tmp_path, [record()])

    assert first.path.parent == collection(tmp_path).sources_root
    assert first.path.name == "20260723T184501.123456Z"
    assert second.path.name == "20260723T184501.123456Z-2"
    assert first.captures_path.read_bytes() == second.captures_path.read_bytes()


def test_concurrent_search_files_publish_without_replacement(tmp_path):
    captures = [record()]
    selected_layout = collection(tmp_path)

    def publish():
        return source_files.save_search_results(
            captures,
            layout=selected_layout,
            url_pattern="https://example.com/*",
            date_start="1995",
            date_end="20260723184501",
            acquired_at=ACQUIRED_AT,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: publish(), range(2)))

    assert {result.path.name for result in results} == {
        "20260723T184501.123456Z",
        "20260723T184501.123456Z-2",
    }
    assert all(result.query_path.exists() for result in results)


def test_search_files_created_during_publication_is_not_replaced(
    tmp_path,
    monkeypatch,
):
    raced = False

    def publish_with_race(source, destination):
        nonlocal raced
        if not raced:
            raced = True
            destination.mkdir()
        publish_directory_noreplace(source, destination)

    monkeypatch.setattr(
        source_files,
        "publish_directory_noreplace",
        publish_with_race,
    )

    result = save(tmp_path, [record()])

    base = result.path.parent / "20260723T184501.123456Z"
    assert result.path.name == "20260723T184501.123456Z-2"
    assert list(base.iterdir()) == []
    assert result.captures_path.exists()


def test_failed_publication_cleans_temporary_directory(
    tmp_path,
    monkeypatch,
):
    def fail(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(source_files, "publish_directory_noreplace", fail)

    with pytest.raises(OSError, match="publication failed"):
        save(tmp_path, [record()])

    source_root = collection(tmp_path).sources_root
    assert list(source_root.iterdir()) == []


def test_empty_source_acquisition_is_rejected_without_output(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        save(tmp_path, [])

    assert not (tmp_path / "archives").exists()
