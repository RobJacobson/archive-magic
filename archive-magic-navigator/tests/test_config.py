from pathlib import Path

import yaml

from archive_magic_navigator.collections import Archive, ReplayCollection
from archive_magic_navigator.config import (
    WAYBACK_MEMENTO_SOURCE,
    WAYBACK_TIMEOUT_SECONDS,
    build_config,
    package_asset_paths,
    render_config,
    write_config,
)


def make_archive(root, archive_id, collection_ids=("2008",)):
    archive_root = root / archive_id
    collections = tuple(
        ReplayCollection(
            collection_id,
            archive_root / "collections" / collection_id,
            archive_root
            / "collections"
            / collection_id
            / f"{archive_id}-{collection_id}-index.cdxj",
        )
        for collection_id in collection_ids
    )
    return Archive(archive_id, archive_root, collections)


def test_single_archive_config_is_safe_and_deterministic(tmp_path):
    root = (tmp_path / "archives").resolve()
    archive = make_archive(root, "example.org", ("2008", "2009"))

    first = build_config([archive])
    second = build_config([archive])

    assert render_config(first) == render_config(second)
    loaded = yaml.safe_load(render_config(first))
    assert loaded["enable_auto_colls"] is False
    assert loaded["framed_replay"] is True
    assert loaded["client_side_replay"] is False
    sequence = loaded["collections"]["example.org"]["sequence"]
    assert sequence[0]["name"] == "local"
    assert sequence[0]["index_group"] == {
        item.collection_id: str(item.replay_index) for item in archive.collections
    }
    assert sequence[0]["archive_paths"] == [
        str(item.root) + "/" for item in archive.collections
    ]
    assert sequence[1] == {
        "name": "wayback",
        "index_group": {"ia": WAYBACK_MEMENTO_SOURCE},
        "timeout": WAYBACK_TIMEOUT_SECONDS,
    }
    assert_forbidden_modes_absent(loaded)


def test_disabled_wayback_fallback_preserves_local_only_config(tmp_path):
    root = (tmp_path / "archives").resolve()
    archive = make_archive(root, "example.org")

    config = build_config([archive], wayback_fallback=False)

    assert config["collections"]["example.org"] == {
        "index_group": {
            item.collection_id: str(item.replay_index) for item in archive.collections
        },
        "archive_paths": [str(item.root) + "/" for item in archive.collections],
    }
    assert WAYBACK_MEMENTO_SOURCE not in render_config(config)


def test_multiple_archive_config_lists_only_validated_archives(tmp_path):
    root = (tmp_path / "archives").resolve()
    archives = [
        make_archive(root, "archive-a"),
        make_archive(root, "archive-b"),
    ]
    config = build_config(archives)

    assert config["enable_auto_colls"] is False
    assert list(config["collections"]) == ["archive-a", "archive-b"]
    assert config["framed_replay"] is True
    assert config["client_side_replay"] is False
    for archive in archives:
        sequence = config["collections"][archive.archive_id][
            "sequence"
        ]
        assert sequence[0]["index_group"] == {
            item.collection_id: str(item.replay_index)
            for item in archive.collections
        }
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
