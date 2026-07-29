from pathlib import Path

import pytest

from archive_magic_navigator.collections import (
    discover_collections,
    resolve_archives_root,
    select_collection,
    validate_collection_id,
)
from archive_magic_navigator.errors import ValidationError


@pytest.mark.parametrize(
    "value",
    ("", ".", "..", "../escape", "nested/name", r"nested\name", "/absolute", "bad\x00id"),
)
def test_invalid_collection_ids(value):
    with pytest.raises(ValidationError, match="invalid collection ID"):
        validate_collection_id(value)


def test_select_collection_resolves_direct_child(collection_factory):
    root, collection, _, _ = collection_factory()

    selected = select_collection(resolve_archives_root(root), "example.org")

    assert selected.collection_id == "example.org"
    assert selected.root == collection.resolve()


def test_discovery_is_sorted_and_ignores_files(collection_factory):
    root, _, _, _ = collection_factory("z.example")
    collection_factory("a.example")
    (root / "README.txt").write_text("not a collection")

    discovered = discover_collections(resolve_archives_root(root))

    assert [item.collection_id for item in discovered] == [
        "a.example",
        "z.example",
    ]


def test_discovery_rejects_escaping_directory_symlink(
    collection_factory,
    tmp_path,
):
    root, _, _, _ = collection_factory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escaped").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValidationError, match="escapes archives root"):
        discover_collections(resolve_archives_root(root))


def test_missing_or_empty_archives_root_fails(tmp_path):
    with pytest.raises(ValidationError, match="does not exist"):
        resolve_archives_root(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValidationError, match="no collection"):
        discover_collections(empty.resolve())


def test_collection_id_is_not_an_arbitrary_path(tmp_path):
    root = tmp_path / "archives"
    root.mkdir()

    with pytest.raises(ValidationError, match="invalid collection ID"):
        select_collection(root.resolve(), str(Path(tmp_path / "elsewhere")))
