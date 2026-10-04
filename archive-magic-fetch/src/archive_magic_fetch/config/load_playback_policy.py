"""Load host acquisition policy with environment and default-path precedence."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from archive_magic_fetch.config.models import (
    INSTANCE_CONFIG_ENV,
    INSTANCE_CONFIG_NAME,
    PlaybackPolicy,
)
from archive_magic_fetch.config.read_toml_section import read_section


def load_playback_policy(value: Path | str | None = None) -> PlaybackPolicy:
    """Load host-level playback policy, or code defaults when no file exists."""

    source = _instance_config_path(value)
    if source is None:
        return PlaybackPolicy()
    try:
        with source.open("rb") as stream:
            document = tomllib.load(stream)
        options = PlaybackPolicy(**read_section(document, "playback", required=False))
        if options.workers <= 0:
            raise ValueError("playback.workers must be positive")
        if float(options.starts_per_second) <= 0:
            raise ValueError("playback.starts_per_second must be positive")
        if options.retries < 0:
            raise ValueError("playback.retries must be nonnegative")
        if document:
            raise TypeError(f"unexpected table(s): {', '.join(sorted(document))}")
    except (
        OSError,
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
    ) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            "fetch instance configuration does not exist:"
        ):
            raise
        raise ValueError(
            f"invalid fetch instance configuration {source}: {error}"
        ) from error
    return PlaybackPolicy(
        workers=options.workers,
        starts_per_second=float(options.starts_per_second),
        retries=options.retries,
    )


def _default_instance_config_path() -> Path:
    """Return the default host-level Fetch policy path."""

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return (base / "archive-magic-fetch" / INSTANCE_CONFIG_NAME).resolve()


def _instance_config_path(value: Path | str | None = None) -> Path | None:
    """Resolve an explicit, env, or default instance config path."""

    if value is not None:
        candidate = Path(value).expanduser()
        if not candidate.is_file():
            raise ValueError(
                f"fetch instance configuration does not exist: {candidate}"
            )
        return candidate.resolve()
    env = os.environ.get(INSTANCE_CONFIG_ENV)
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_file():
            raise ValueError(
                f"fetch instance configuration does not exist: {candidate}"
            )
        return candidate.resolve()
    default = _default_instance_config_path()
    return default if default.is_file() else None
