from pathlib import Path

import pytest

from archive_magic_navigator.collections import (
    discover_archives,
    resolve_archives_root,
    select_archive,
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


def test_select_archive_resolves_domain_and_portable_collections(collection_factory):
    root, archive, _, _ = collection_factory()

    selected = select_archive(resolve_archives_root(root), "example.org")

    assert selected.archive_id == "example.org"
    assert selected.root == archive.resolve()
    assert [item.collection_id for item in selected.collections] == ["2020"]


def test_discovery_is_sorted_and_ignores_files(collection_factory):
    root, _, _, _ = collection_factory("z.example")
    collection_factory("a.example")
    (root / "README.txt").write_text("not a collection")

    discovered = discover_archives(resolve_archives_root(root))

    assert [item.archive_id for item in discovered] == [
        "a.example",
        "z.example",
    ]


def test_archive_discovery_ignores_capture_metadata(collection_factory):
    root, archive, _, _ = collection_factory()
    run = archive / "captures" / "2020" / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}\n", encoding="utf-8")

    selected = select_archive(resolve_archives_root(root), "example.org")

    assert [item.collection_id for item in selected.collections] == ["2020"]


def test_discovery_rejects_escaping_directory_symlink(
    collection_factory,
    tmp_path,
):
    root, _, _, _ = collection_factory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="invalid archives"):
        discover_archives(resolve_archives_root(root))


def test_missing_or_empty_archives_root_fails(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        resolve_archives_root(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValidationError, match="no domain archive"):
        discover_archives(empty.resolve())


def test_archive_id_is_not_an_arbitrary_path(tmp_path):
    root = tmp_path / "archives"
    root.mkdir()

    with pytest.raises(ValidationError, match="invalid archive ID"):
        select_archive(root.resolve(), str(Path(tmp_path / "elsewhere")))


def test_archive_id_reserves_static():
    with pytest.raises(ValidationError, match="invalid archive ID"):
        validate_archive_id("static")
