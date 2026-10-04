"""Load per-source host acquisition policy, creating missing configuration."""

from __future__ import annotations

import math
import os
import tomllib
from pathlib import Path

from archive_magic_fetch.config.models import (
    INSTANCE_CONFIG_ENV,
    INSTANCE_CONFIG_NAME,
    PlaybackPolicy,
)
from archive_magic_fetch.config.read_toml_section import read_section


SOURCES = ("wayback", "common-crawl")


def load_playback_policy(
    value: Path | str | None = None, *, source: str = "wayback"
) -> PlaybackPolicy:
    """Create missing host configuration and load the selected source's policy."""

    if source not in SOURCES:
        raise ValueError(f"unknown acquisition source: {source}")
    path = _instance_config_path(value)
    try:
        _create_instance_config(path)
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        policies = {
            name: _read_policy(read_section(document, name), name)
            for name in SOURCES
        }
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"invalid fetch instance configuration {path}: {error}"
        ) from error
    return policies[source]


def _read_policy(options: dict[str, object], source: str) -> PlaybackPolicy:
    expected = {"workers", "starts_per_second", "retries"}
    missing = expected - options.keys()
    extra = options.keys() - expected
    if missing or extra:
        raise ValueError(
            f"{source}: missing settings {sorted(missing)}; "
            f"unexpected settings {sorted(extra)}"
        )
    workers = options["workers"]
    rate = options["starts_per_second"]
    retries = options["retries"]
    if type(workers) is not int or workers <= 0:
        raise ValueError(f"{source}.workers must be a positive integer")
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"{source}.starts_per_second must be a finite positive number")
    if type(retries) is not int or retries < 0:
        raise ValueError(f"{source}.retries must be a nonnegative integer")
    return PlaybackPolicy(workers=workers, starts_per_second=float(rate), retries=retries)


def _create_instance_config(path: Path) -> None:
    """Write complete defaults only when the selected file does not exist."""

    if path.exists():
        return
    defaults = PlaybackPolicy()
    contents = "\n".join(
        f"[{source}]\n"
        f"workers = {defaults.workers}\n"
        f"starts_per_second = {defaults.starts_per_second:g}\n"
        f"retries = {defaults.retries}\n"
        for source in SOURCES
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(contents)
    except FileExistsError:
        # Another process created the file; preserve its settings.
        pass


def _default_instance_config_path() -> Path:
    """Return the default host-level Fetch policy path."""

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / "archive-magic-fetch" / INSTANCE_CONFIG_NAME).resolve()


def _instance_config_path(value: Path | str | None = None) -> Path:
    """Resolve an explicit, env, or default instance config path."""

    if value is not None:
        return Path(value).expanduser().resolve()
    env = os.environ.get(INSTANCE_CONFIG_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return _default_instance_config_path()
