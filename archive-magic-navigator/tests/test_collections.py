from pathlib import Path

import pytest

from archive_magic_navigator.collections import (
    select_archive_root,
    validate_archive_id,
    validate_collection_id,
)
from archive_magic_navigator.errors import ValidationError


@pytest.mark.parametrize(
    "value",
    (
        "",
        ".",
        "..",
        "../escape",
        "nested/name",
        r"nested\name",
        "/absolute",
        "bad\x00id",
        "query?id",
        "fragment#id",
        "line\nbreak",
        "café",
        "$root",
    ),
)
def test_invalid_collection_ids(value):
    with pytest.raises(ValidationError, match="invalid collection ID"):
        validate_collection_id(value)


@pytest.mark.parametrize(
    "value",
    ("example.org", "collection-1", "collection_name", "A.B", "static"),
)
def test_route_safe_collection_ids(value):
    assert validate_collection_id(value) == value


def test_select_archive_root_resolves_portable_collections(collection_factory):
    _, archive, _, _ = collection_factory()

    selected = select_archive_root(archive, "example.org")

    assert selected.archive_id == "example.org"
    assert selected.root == archive.resolve()
    assert [item.collection_id for item in selected.collections] == ["2020"]


def test_archive_discovery_ignores_capture_metadata(collection_factory):
    _, archive, _, _ = collection_factory()
    run = archive.parent / "logs" / "run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}\n", encoding="utf-8")

    selected = select_archive_root(archive, "example.org")

    assert [item.collection_id for item in selected.collections] == ["2020"]


def test_select_archive_root_lists_multiple_portable_collections_sorted(
    collection_factory,
):
    _, archive, _, _ = collection_factory(collection_id="2020")
    collection_factory(collection_id="2008")
    run = archive.parent / "logs" / "run-noise"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}\n", encoding="utf-8")

    selected = select_archive_root(archive, "example.org")

    assert [item.collection_id for item in selected.collections] == ["2008", "2020"]


def test_unindexed_collection_dir_is_skipped(collection_factory):
    _, archive, _, _ = collection_factory()
    (archive / "example.org-2004-001.warc.gz.partial").write_bytes(b"partial")

    selected = select_archive_root(archive, "example.org")

    assert [item.collection_id for item in selected.collections] == ["2020"]


def test_archive_with_only_unindexed_year_is_not_playable(tmp_path):
    archive = tmp_path / "example.org"
    archive.mkdir(parents=True)
    (archive / "example.org-2004-001.warc.gz.partial").write_bytes(b"partial")

    with pytest.raises(ValidationError, match="no playable collections"):
        select_archive_root(archive, "example.org")


def test_select_archive_root_rejects_escaping_index_symlink(
    collection_factory,
    tmp_path,
):
    _, archive, _, _ = collection_factory()
    outside = tmp_path / "outside.cdxj"
    outside.write_text("x\n", encoding="utf-8")
    (archive / "example.org-escaped-index.cdxj").symlink_to(outside)

    with pytest.raises(ValidationError, match="not contained"):
        select_archive_root(archive, "example.org")


def test_missing_workspace_fails(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        select_archive_root(tmp_path / "missing", "example.org")


def test_archive_id_reserves_static():
    with pytest.raises(ValidationError, match="invalid archive ID"):
        validate_archive_id("static")
