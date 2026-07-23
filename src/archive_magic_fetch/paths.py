"""Safe deterministic output paths for exact resource URLs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Mapping, Union
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
) -> Path:
    """Map an exact capture URL to its WARC path beneath *root*."""

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

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
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


def preflight_paths(
    urls: Union[Iterable[str], Mapping[str, object]],
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Path]:
    """Compute all output paths and reject conflicts before retrieval."""

    output_paths = {}
    generated_paths = {}

    for url in urls:
        path = warc_path(url, root=root)
        previous_url = generated_paths.get(path)
        if previous_url is not None and previous_url != url:
            raise ValueError(
                f"output path collision for {previous_url} and {url}: {path}"
            )

        generated_paths[path] = url
        output_paths[url] = path

        try:
            os.lstat(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise OSError(f"cannot inspect output path for {url}: {path}") from error
        else:
            raise FileExistsError(f"output file already exists: {path}")

    return output_paths

