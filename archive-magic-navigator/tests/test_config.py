from pathlib import Path

import yaml

from archive_magic_navigator.collections import Collection
from archive_magic_navigator.config import (
    WAYBACK_MEMENTO_SOURCE,
    WAYBACK_TIMEOUT_SECONDS,
    build_config,
    package_asset_paths,
    render_config,
    write_config,
)


def test_single_collection_config_is_safe_and_deterministic(tmp_path):
    root = (tmp_path / "archives").resolve()
    collection = Collection("example.org", root / "example.org")

    first = build_config([collection])
    second = build_config([collection])

    assert render_config(first) == render_config(second)
    loaded = yaml.safe_load(render_config(first))
    assert loaded["enable_auto_colls"] is False
    assert loaded["framed_replay"] is True
    assert loaded["client_side_replay"] is False
    sequence = loaded["collections"]["example.org"]["sequence"]
    assert sequence[0]["name"] == "local"
    assert sequence[0]["index"].endswith(
        "/example.org/indexes/index.cdxj"
    )
    assert sequence[0]["archive_paths"] == [
        str(collection.root) + "/"
    ]
    assert sequence[1] == {
        "name": "wayback",
        "index_group": {"ia": WAYBACK_MEMENTO_SOURCE},
        "timeout": WAYBACK_TIMEOUT_SECONDS,
    }
    assert_forbidden_modes_absent(loaded)


def test_disabled_wayback_fallback_preserves_local_only_config(tmp_path):
    root = (tmp_path / "archives").resolve()
    collection = Collection("example.org", root / "example.org")

    config = build_config([collection], wayback_fallback=False)

    assert config["collections"]["example.org"] == {
        "index": str(collection.replay_index),
        "archive_paths": [str(collection.root) + "/"],
    }
    assert WAYBACK_MEMENTO_SOURCE not in render_config(config)


def test_multiple_collection_config_lists_only_validated_collections(tmp_path):
    root = (tmp_path / "archives").resolve()
    collections = [
        Collection("collection-a", root / "collection-a"),
        Collection("collection-b", root / "collection-b"),
    ]
    config = build_config(collections)

    assert config["enable_auto_colls"] is False
    assert list(config["collections"]) == ["collection-a", "collection-b"]
    assert "collections_root" not in config
    assert "index_paths" not in config
    assert config["framed_replay"] is True
    assert config["client_side_replay"] is False
    for collection in collections:
        sequence = config["collections"][collection.collection_id][
            "sequence"
        ]
        assert sequence[0]["index"] == str(collection.replay_index)
        assert sequence[1]["index_group"] == {
            "ia": WAYBACK_MEMENTO_SOURCE
        }
    assert_forbidden_modes_absent(config)


def test_assets_are_packaged_outside_archives(tmp_path):
    templates, static = package_asset_paths()
    root = (tmp_path / "archives").resolve()

    assert (templates / "index.html").is_file()
    assert (templates / "search.html").is_file()
    assert (static / "archive-magic.css").is_file()
    for path in (templates, static):
        assert root not in path.parents


def test_write_config_only_writes_runtime_directory(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = write_config(runtime, {"framed_replay": True})

    assert path == runtime / "config.yaml"
    assert yaml.safe_load(path.read_text()) == {"framed_replay": True}
    assert (runtime / "templates" / "index.html").is_file()
    assert (runtime / "templates" / "search.html").is_file()
    assert (runtime / "static" / "archive-magic.css").is_file()


def assert_forbidden_modes_absent(config):
    text = render_config(config)
    for forbidden in (
        "recorder",
        "$live",
        "autoindex",
        "proxy:",
        "enable_auto_fetch",
    ):
        assert forbidden not in text
