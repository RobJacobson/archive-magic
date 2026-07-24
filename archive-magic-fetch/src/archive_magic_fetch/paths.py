"""Safe collection layout and deterministic WARC bucket allocation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, Union
from urllib.parse import quote, urlsplit


DEFAULT_OUTPUT_ROOT = Path("../archives")
MAX_COMPONENT_BYTES = 240
_POSIX_NAME_MAX_FALLBACK = 255
_POSIX_PATH_MAX_FALLBACK = 1024
_WINDOWS_NAME_MAX_FALLBACK = 255
_WINDOWS_PATH_MAX_FALLBACK = 260
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_PARTIAL_ESCAPE = re.compile(r"%(?:[0-9A-F])?$")


@dataclass(frozen=True)
class CollectionLayout:
    """The filesystem boundaries for one requested website collection."""

    root: Path
    name: str

    @property
    def collection_root(self) -> Path:
        return self.root / self.name

    @property
    def sources_root(self) -> Path:
        return self.collection_root / "sources"

    @property
    def wayback_sources_root(self) -> Path:
        return self.sources_root / "wayback"

    @property
    def archive_root(self) -> Path:
        return self.collection_root / "archive"

    @property
    def replay_index(self) -> Path:
        return self.collection_root / "replay" / "index.cdxj"


@dataclass(frozen=True)
class WarcBucket:
    """One WARC path and the URL-key groups assigned to it."""

    path: Path
    urlkeys: tuple[str, ...]


@dataclass(frozen=True)
class ExportPlan:
    """Preflighted collection layout and ordered WARC buckets."""

    layout: CollectionLayout
    buckets: tuple[WarcBucket, ...]


def _safe_segment(component: str) -> str:
    """Encode one value as one recognizable filesystem component."""

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


def _bounded_segment(component: str, *, byte_limit: int) -> str:
    """Safely encode and truncate a component without splitting an escape."""

    encoded = _safe_segment(component)
    if len(encoded) <= byte_limit:
        return encoded

    encoded = _truncate_escape_safe(encoded, byte_limit)
    trailing_dots = len(encoded) - len(encoded.rstrip("."))
    if trailing_dots:
        replacement = "%2E" * trailing_dots
        prefix = _truncate_escape_safe(
            encoded[:-trailing_dots],
            byte_limit - len(replacement),
        )
        encoded = prefix + replacement
    if not encoded:  # pragma: no cover - byte limits are deliberately large
        raise ValueError("filesystem component limit is too small")
    return encoded


def _truncate_escape_safe(encoded: str, byte_limit: int) -> str:
    """Truncate canonical ASCII encoding without leaving a partial escape."""

    encoded = encoded[:byte_limit]
    partial = _PARTIAL_ESCAPE.search(encoded)
    if partial is not None:
        encoded = encoded[: partial.start()]
    return encoded


def normalize_collection_name(url_pattern: str) -> str:
    """Derive one safe collection directory from a supported URL pattern."""

    if not isinstance(url_pattern, str) or not url_pattern.strip():
        raise ValueError("URL pattern must be a non-empty string")

    pattern = url_pattern.strip()
    if pattern.startswith("*."):
        pattern = pattern[2:]

    has_scheme = "://" in pattern
    parsed = urlsplit(pattern if has_scheme else f"//{pattern}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL pattern user information is not supported")

    host = parsed.hostname
    if not host or "*" in host:
        raise ValueError(
            f"URL pattern must identify one unambiguous website host: "
            f"{url_pattern}"
        )

    host = host.rstrip(".").lower()
    if not host:
        raise ValueError("URL pattern host cannot be empty")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError(f"URL pattern host is not valid IDNA: {host}") from error

    if host.startswith("www."):
        host = host[4:]
    if not host:
        raise ValueError("URL pattern host cannot be only www")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"URL pattern has an invalid port: {url_pattern}") from error

    scheme = parsed.scheme.lower()
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        host = f"{host}--port-{port}"

    name = _safe_segment(host)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"URL pattern produced an unsafe collection name: {name}")
    if len(name.encode("ascii")) > MAX_COMPONENT_BYTES:
        raise ValueError(
            f"collection name exceeds {MAX_COMPONENT_BYTES} encoded bytes"
        )
    return name


def collection_layout(
    url_pattern: str,
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
) -> CollectionLayout:
    """Build and validate the collection boundary for a command."""

    layout = CollectionLayout(Path(root), normalize_collection_name(url_pattern))
    validate_path_limits(layout.collection_root)
    return layout


def preferred_warc_path(urlkey: str, layout: CollectionLayout) -> Path:
    """Map a CDX resource family to its preferred readable WARC bucket."""

    if not isinstance(urlkey, str) or not urlkey:
        raise ValueError("CDX urlkey must be a non-empty string")

    authority_end = urlkey.find(")")
    if authority_end < 0:
        raise ValueError(f"CDX urlkey has no SURT authority terminator: {urlkey}")
    resource = urlkey[authority_end + 1 :]
    if not resource.startswith("/"):
        raise ValueError(f"CDX urlkey has no absolute resource path: {urlkey}")

    path_resource, has_query, query = resource.partition("?")
    segments = path_resource[1:].split("/")
    if path_resource.endswith("/"):
        directory_segments = segments[:-1]
        stem = "index"
    else:
        directory_segments = segments[:-1]
        stem = segments[-1]
    if has_query:
        stem = f"{stem}?{query}"

    suffix = ".warc.gz"
    stem_limit = MAX_COMPONENT_BYTES - len(suffix)
    filename = f"{_bounded_segment(stem, byte_limit=stem_limit)}{suffix}"
    return layout.archive_root.joinpath(
        *(
            _bounded_segment(segment, byte_limit=MAX_COMPONENT_BYTES)
            for segment in directory_segments
        ),
        filename,
    )


def _equivalent_component(component: str) -> str:
    """Return a conservative cross-filesystem identity for one component."""

    return component.rstrip(" .").casefold()


def _equivalent_path(path: Path, root: Path) -> tuple[str, ...]:
    relative = path.relative_to(root)
    return tuple(_equivalent_component(part) for part in relative.parts)


def _nearest_existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        if candidate.is_symlink():
            raise OSError(f"broken symlink in output path: {candidate}")
        parent = candidate.parent
        if parent == candidate:
            return candidate
        candidate = parent
    return candidate


def _pathconf_limit(path: Path, name: str, fallback: int) -> int:
    try:
        value = os.pathconf(path, name)
    except (AttributeError, OSError, ValueError):
        return fallback
    if not isinstance(value, int) or value <= 0:
        return fallback
    return value


def validate_path_limits(path: Path) -> None:
    """Validate both per-component and complete-path filesystem limits."""

    absolute = path.absolute()
    existing = _nearest_existing_ancestor(absolute)
    if os.name == "nt":
        name_max = _WINDOWS_NAME_MAX_FALLBACK
        path_max = _WINDOWS_PATH_MAX_FALLBACK
        component_lengths = [(part, len(part)) for part in absolute.parts]
        total_length = len(str(absolute)) + 1
    else:
        name_max = _pathconf_limit(
            existing,
            "PC_NAME_MAX",
            _POSIX_NAME_MAX_FALLBACK,
        )
        path_max = _pathconf_limit(
            existing,
            "PC_PATH_MAX",
            _POSIX_PATH_MAX_FALLBACK,
        )
        component_lengths = [
            (part, len(os.fsencode(part))) for part in absolute.parts
        ]
        total_length = len(os.fsencode(str(absolute))) + 1

    for component, length in component_lengths:
        if length > name_max:
            raise OSError(
                f"output path component exceeds NAME_MAX ({name_max}): "
                f"{component}"
            )
    if total_length > path_max:
        raise OSError(
            f"output path exceeds PATH_MAX ({path_max}): {absolute}"
        )


def _inspect_target(path: Path) -> None:
    """Reject an existing final target or an unusable ancestor."""

    inspect_error = None
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        inspect_error = error
    else:
        raise FileExistsError(f"output file already exists: {path}")

    ancestor = path.parent
    while ancestor != ancestor.parent:
        if ancestor.is_symlink() and not ancestor.exists():
            raise OSError(f"broken symlink in output path: {ancestor}")
        if ancestor.exists():
            if not ancestor.is_dir():
                raise OSError(f"output ancestor is not a directory: {ancestor}")
            break
        ancestor = ancestor.parent

    if inspect_error is not None:
        raise OSError(f"cannot inspect output target: {path}") from inspect_error
    validate_path_limits(path)


def preflight_layout(
    capture_groups: Mapping[str, Sequence[object]],
    layout: CollectionLayout,
) -> ExportPlan:
    """Allocate collision buckets and inspect all final targets."""

    candidates: dict[tuple[str, ...], list[tuple[Path, str]]] = {}
    for urlkey, captures in capture_groups.items():
        if not captures:
            raise ValueError(f"capture group is empty: {urlkey}")
        path = preferred_warc_path(urlkey, layout)
        key = _equivalent_path(path, layout.collection_root)
        candidates.setdefault(key, []).append((path, urlkey))

    exact_buckets = []
    for key, equivalent_candidates in candidates.items():
        ordered = sorted(
            equivalent_candidates,
            key=lambda item: (
                item[0].relative_to(layout.collection_root).as_posix(),
                item[1],
            ),
        )
        final_path = ordered[0][0]
        urlkeys = sorted(urlkey for _, urlkey in ordered)
        exact_buckets.append((key, final_path, urlkeys))

    # A planned WARC may otherwise become the directory ancestor of another
    # planned WARC. Assign descendant groups to the shortest conflicting WARC
    # so every conflict is resolved before playback begins.
    exact_buckets.sort(
        key=lambda item: (
            len(item[0]),
            item[0],
            item[1].relative_to(layout.collection_root).as_posix(),
        )
    )
    allocated: list[tuple[tuple[str, ...], Path, list[str]]] = []
    for key, final_path, urlkeys in exact_buckets:
        ancestor = next(
            (
                bucket
                for bucket in allocated
                if len(bucket[0]) < len(key)
                and key[: len(bucket[0])] == bucket[0]
            ),
            None,
        )
        if ancestor is not None:
            ancestor[2].extend(urlkeys)
        else:
            allocated.append((key, final_path, urlkeys))

    buckets = [
        WarcBucket(path, tuple(sorted(urlkeys)))
        for _, path, urlkeys in allocated
    ]
    buckets.sort(
        key=lambda bucket: bucket.path.relative_to(
            layout.collection_root
        ).as_posix()
    )
    for bucket in buckets:
        _inspect_target(bucket.path)
    _inspect_target(layout.replay_index)
    return ExportPlan(layout, tuple(buckets))
