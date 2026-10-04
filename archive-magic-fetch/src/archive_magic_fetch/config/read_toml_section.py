"""Shared TOML table validation."""

from __future__ import annotations


def read_section(
    document: dict[str, object], name: str, *, required: bool = True
) -> dict[str, object]:
    value = document.pop(name) if required else document.pop(name, {})
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be a TOML table")
    return value
