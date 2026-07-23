"""Safe deterministic output paths for CDX URL-key resource groups."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping, Sequence, Union
from urllib.parse import quote, urlsplit


DEFAULT_OUTPUT_ROOT = Path("warcs")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _safe_segment(component: str) -> str:
    """Encode one URL component as one recognizable filesystem segment."""

    if component == "":
        return "%00"
    if component == ".":
        return "%2E"
    if component == "..":
        return "%2E%2E"

    encoded = quote(component, safe="-._~", encoding="utf-8", errors="strict")

    trailing_dots = len(encoded) - len(encoded.rstrip("."))
    if trailing_dots:
        encoded = encoded[:-trailing_dots] + "%2E" * trailing_dots

    reserved_base = encoded.split(".", 1)[0].upper()
    if reserved_base in _WINDOWS_RESERVED_NAMES:
        first_byte = encoded[0].encode("ascii")[0]
        encoded = f"%{first_byte:02X}{encoded[1:]}"

    return encoded


def _host_with_port(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"capture URL must include a scheme and host: {url}")

    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return parsed.scheme, host


def warc_path(
    url: str,
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
    identity: str | None = None,
) -> Path:
    """Map a representative URL and stable group identity beneath *root*."""

    parsed = urlsplit(url)
    scheme, host = _host_with_port(url)
    path_segments = parsed.path.split("/")
    if path_segments and path_segments[0] == "":
        path_segments = path_segments[1:]

    if not parsed.path or parsed.path.endswith("/"):
        directory_segments = path_segments[:-1] if path_segments else []
        stem = "index"
    else:
        directory_segments = path_segments[:-1]
        stem = path_segments[-1]

    hash_input = identity if identity is not None else url
    url_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:12]
    filename = f"{_safe_segment(stem)}--{url_hash}.warc.gz"

    root_path = Path(root)
    candidate = root_path.joinpath(
        _safe_segment(scheme),
        _safe_segment(host),
        *(_safe_segment(segment) for segment in directory_segments),
        filename,
    )

    try:
        candidate.relative_to(root_path)
    except ValueError as error:  # pragma: no cover - defensive invariant
        raise ValueError(f"generated path escapes output root for {url}") from error

    return candidate


def urlkey_warc_path(
    urlkey: str,
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
) -> Path:
    """Map a CDX URL key to one stable, recognizable WARC path."""

    if not isinstance(urlkey, str) or not urlkey:
        raise ValueError("CDX urlkey must be a non-empty string")

    key_segments = urlkey.split("/")
    if urlkey.endswith("/"):
        directory_segments = key_segments[:-1]
        stem = "index"
    else:
        directory_segments = key_segments[:-1]
        stem = key_segments[-1]

    urlkey_hash = hashlib.sha256(urlkey.encode("utf-8")).hexdigest()[:12]
    filename = f"{_safe_segment(stem)}--{urlkey_hash}.warc.gz"
    root_path = Path(root)
    candidate = root_path.joinpath(
        "urlkey",
        *(_safe_segment(segment) for segment in directory_segments),
        filename,
    )

    try:
        candidate.relative_to(root_path)
    except ValueError as error:  # pragma: no cover - defensive invariant
        raise ValueError(
            f"generated path escapes output root for {urlkey}"
        ) from error

    return candidate


def preflight_paths(
    capture_groups: Mapping[str, Sequence[Mapping]],
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Path]:
    """Compute one output path per CDX URL-key group before retrieval."""

    output_paths = {}
    generated_paths = {}

    for urlkey, captures in capture_groups.items():
        if not captures:
            raise ValueError(f"capture group is empty: {urlkey}")

        path = urlkey_warc_path(urlkey, root=root)
        previous_urlkey = generated_paths.get(path)
        if previous_urlkey is not None and previous_urlkey != urlkey:
            raise ValueError(
                f"output path collision for {previous_urlkey} and {urlkey}: {path}"
            )

        generated_paths[path] = urlkey
        output_paths[urlkey] = path

        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise OSError(
                f"cannot inspect output path for {urlkey}: {path}"
            ) from error
        else:
            raise FileExistsError(f"output file already exists: {path}")

    return output_paths
