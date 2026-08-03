"""Deterministic pywb configuration generation."""

from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .collections import Collection


WAYBACK_MEMENTO_SOURCE = "memento+https://web.archive.org/web/"
WAYBACK_TIMEOUT_SECONDS = 10


def _collection_config(
    collection: Collection,
    *,
    wayback_fallback: bool,
) -> dict[str, Any]:
    local = {
        "index": str(collection.replay_index),
        "archive_paths": [str(collection.root) + os.sep],
    }
    if not wayback_fallback:
        return local

    return {
        "sequence": [
            {"name": "local", **local},
            {
                "name": "wayback",
                "index_group": {"ia": WAYBACK_MEMENTO_SOURCE},
                "timeout": WAYBACK_TIMEOUT_SECONDS,
            },
        ]
    }


def package_asset_paths() -> tuple[Path, Path]:
    """Return absolute installed template and static directories."""

    package_root = resources.files("archive_magic_navigator")
    templates = Path(str(package_root.joinpath("templates"))).resolve()
    static = Path(str(package_root.joinpath("static"))).resolve()
    return templates, static


def build_config(
    collections: Sequence[Collection],
    *,
    wayback_fallback: bool = True,
) -> dict[str, Any]:
    """Build one safe pywb configuration from validated inputs."""

    return {
        "enable_auto_colls": False,
        "collections": {
            collection.collection_id: _collection_config(
                collection,
                wayback_fallback=wayback_fallback,
            )
            for collection in collections
        },
        "framed_replay": True,
        "client_side_replay": False,
        "templates_dir": "templates",
        "static_dir": "static",
    }


def render_config(config: dict[str, Any]) -> str:
    """Serialize safe YAML with stable key ordering."""

    return yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def write_config(runtime_directory: Path, config: dict[str, Any]) -> Path:
    """Write the ephemeral config consumed by the child process."""

    stage_assets(runtime_directory)
    path = runtime_directory / "config.yaml"
    path.write_text(render_config(config), encoding="utf-8")
    return path


def stage_assets(runtime_directory: Path) -> None:
    """Copy the tiny packaged overrides into pywb's runtime search paths."""

    templates, static = package_asset_paths()
    shutil.copytree(templates, runtime_directory / "templates")
    shutil.copytree(static, runtime_directory / "static")
