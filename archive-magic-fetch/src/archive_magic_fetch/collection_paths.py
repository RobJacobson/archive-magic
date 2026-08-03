"""Safe collection paths and deterministic WARC file allocation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional, Sequence, Union
from urllib.parse import quote, unquote, urlsplit


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
_WWW_ALIAS_PREFIX = re.compile(r"^www\d*\.")
_MIME_SUFFIXES = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/pdf": ".pdf",
    "text/css": ".css",
    "application/javascript": ".js",
    "text/javascript": ".js",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}
UrlHistoryKey = tuple[str, str]


@dataclass(frozen=True)
class CollectionPaths:
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
    def archive_root(self) -> Path:
        return self.collection_root / "archive"

    @property
    def website_root(self) -> Path:
        return self.collection_root / "website"

    @property
    def replay_index(self) -> Path:
        return self.collection_root / "replay" / "index.cdxj"


@dataclass(frozen=True)
class WebsiteFile:
    """One loose-file destination attached directly to its capture."""

    path: Path
    capture: object


@dataclass(frozen=True)
class WebsiteFiles:
    """Preflighted loose-file destinations under ``website/``."""

    paths: CollectionPaths
    targets: tuple[WebsiteFile, ...]


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
    if encoded.endswith("."):
        prefix = _truncate_escape_safe(
            encoded[:-1],
            byte_limit - len("%2E"),
        )
        encoded = prefix + "%2E"
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


def normalize_domain(
    value: str,
    *,
    allow_bare: bool = False,
) -> tuple[str, Optional[int]]:
    """Return normalized host and significant port for one URL."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("URL must be a non-empty string")
    text = value.strip()
    has_scheme = "://" in text
    parsed = urlsplit(text if has_scheme else f"//{text}")
    if not allow_bare and (not parsed.scheme or not parsed.netloc):
        raise ValueError(f"URL must be absolute: {value}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL user information is not supported")

    host = parsed.hostname
    if not host:
        raise ValueError(f"URL must include a host: {value}")
    host = host.rstrip(".").lower()
    if not host:
        raise ValueError("URL host cannot be empty")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError(f"URL host is not valid IDNA: {host}") from error
    host = _WWW_ALIAS_PREFIX.sub("", host, count=1)
    if not host:
        raise ValueError("URL host cannot be only www")

    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"URL has an invalid port: {value}") from error
    scheme = parsed.scheme.lower()
    if (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        port = None
    return host, port


def mime_suffix(value: object) -> Optional[str]:
    """Return the supported conventional suffix for one MIME value."""

    if not isinstance(value, str):
        return None
    normalized = value.split(";", 1)[0].strip().lower()
    return _MIME_SUFFIXES.get(normalized)


def normalize_collection_name(url_pattern: str) -> str:
    """Derive one safe collection directory from a supported URL pattern."""

    if not isinstance(url_pattern, str) or not url_pattern.strip():
        raise ValueError("URL pattern must be a non-empty string")

    pattern = url_pattern.strip()
    if pattern.startswith("*."):
        pattern = pattern[2:]

    host, port = normalize_domain(pattern, allow_bare=True)
    if "*" in host:
        raise ValueError(
            f"URL pattern must identify one unambiguous website host: "
            f"{url_pattern}"
        )
    name = _authority_segment(host, port)
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"URL pattern produced an unsafe collection name: {name}")
    if len(name.encode("ascii")) > MAX_COMPONENT_BYTES:
        raise ValueError(
            f"collection name exceeds {MAX_COMPONENT_BYTES} encoded bytes"
        )
    return name


def collection_paths(
    url_pattern: str,
    root: Union[str, os.PathLike] = DEFAULT_OUTPUT_ROOT,
) -> CollectionPaths:
    """Build and validate the collection boundary for a command."""

    layout = CollectionPaths(Path(root), normalize_collection_name(url_pattern))
    validate_path_limits(layout.collection_root)
    return layout


def preferred_warc_path(
    urlkey: str,
    original_url: str,
    layout: CollectionPaths,
) -> Path:
    """Map one URL history to its preferred readable WARC path."""

    if not isinstance(original_url, str):
        raise TypeError("original_url must be a string")
    domain = domain_folder(original_url)

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
        domain,
        *(
            _bounded_segment(segment, byte_limit=MAX_COMPONENT_BYTES)
            for segment in directory_segments
        ),
        filename,
    )


def _cdx_timestamp_text(timestamp: datetime) -> str:
    """Format an aware capture timestamp as a 14-digit CDX UTC value."""

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("capture timestamp must include a timezone")
    return timestamp.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def domain_folder(original_url: str) -> str:
    """Return the filesystem host segment for one absolute capture URL."""

    host, port = normalize_domain(original_url)
    return _authority_segment(host, port)


def _authority_segment(host: str, port: Optional[int]) -> str:
    """Encode one normalized URL authority as a filesystem component."""

    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return _safe_segment(authority)


def _split_site_path(path: str) -> tuple[list[str], bool]:
    """Return decoded path segments and whether the URL has an explicit suffix.

    Query strings are intentionally ignored for loose-file path planning.
    """

    path = unquote(path or "")
    if path.startswith("/"):
        path = path[1:]
    if path.endswith("/"):
        path = path[:-1]

    segments = [segment for segment in path.split("/") if segment != ""]
    explicit_suffix = bool(segments and "." in segments[-1])
    return segments, explicit_suffix


def _website_path_segments(original_url: str) -> tuple[list[str], bool]:
    """Return decoded path segments and explicit-suffix state."""

    if not isinstance(original_url, str) or not original_url.strip():
        raise ValueError("capture URL must be a non-empty string")

    parsed = urlsplit(original_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"capture URL must be absolute: {original_url}")

    return _split_site_path(parsed.path or "")


def website_relative_parts(
    original_url: str,
    *,
    mimetype: object,
    timestamp: Optional[datetime] = None,
) -> tuple[str, ...]:
    """Build safe relative parts under ``website/`` for one capture URL."""

    host = domain_folder(original_url)
    segments, explicit_suffix = _website_path_segments(original_url)
    parts: list[str] = [host]
    if timestamp is not None:
        parts.append(_cdx_timestamp_text(timestamp))

    suffix = mime_suffix(mimetype)
    if explicit_suffix:
        parts.extend(segments)
    elif suffix == ".html":
        parts.extend(segments)
        parts.append("index.html")
    elif suffix is not None:
        if segments:
            parts.extend(segments[:-1])
            parts.append(f"{segments[-1]}{suffix}")
        else:
            parts.append(f"index{suffix}")
    else:
        parts.extend(segments or ["index"])

    return tuple(
        _bounded_segment(segment, byte_limit=MAX_COMPONENT_BYTES)
        for segment in parts
    )


def preferred_website_path(
    original_url: str,
    layout: CollectionPaths,
    *,
    mimetype: object,
    timestamp: Optional[datetime] = None,
) -> Path:
    """Map one original URL to its preferred loose-file path under website/."""

    return layout.website_root.joinpath(
        *website_relative_parts(
            original_url,
            mimetype=mimetype,
            timestamp=timestamp,
        )
    )


def preferred_site_file(site_root: Path, url_path: str) -> Path:
    """Map a site-absolute path to its preferred file under one site root.

    Query strings are ignored (folded), matching loose-file path planning.
    """

    segments, explicit_suffix = _split_site_path(url_path)
    parts = list(segments)
    if not explicit_suffix:
        parts.append("index.html")
    return site_root.joinpath(
        *(
            _bounded_segment(segment, byte_limit=MAX_COMPONENT_BYTES)
            for segment in parts
        )
    )


def _digest_path_token(digest: object, *, capture_index: int) -> str:
    """Return a short stable token for disambiguating colliding website paths."""

    if isinstance(digest, str):
        token = digest.strip()
        if token and token != "-":
            if ":" in token:
                _algorithm, token = token.split(":", 1)
            cleaned = re.sub(r"[^A-Za-z0-9]", "", token).upper()
            if cleaned:
                return cleaned[:8]
    return f"I{capture_index:04d}"


def _with_filename_token(path: Path, token: str) -> Path:
    """Insert ``--TOKEN`` before the final filename extension."""

    name = path.name
    if "." in name:
        stem, extension = name.rsplit(".", 1)
        filename = f"{stem}--{token}.{extension}"
    else:
        filename = f"{name}--{token}"
    return path.with_name(
        _bounded_segment(filename, byte_limit=MAX_COMPONENT_BYTES)
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


def _inspect_target(
    path: Path,
) -> None:
    """Inspect one output target and its nearest existing ancestor."""

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


def allocate_warc_paths(
    captures_by_url: Mapping[tuple[str, str], Sequence[object]],
    layout: CollectionPaths,
) -> dict[Path, tuple[tuple[str, str], ...]]:
    """Map readable WARC paths to URL-history keys in linear path-depth work."""

    candidates: dict[
        tuple[str, ...],
        list[tuple[str, Path, object]],
    ] = {}
    for history_key, captures in captures_by_url.items():
        if (
            not isinstance(history_key, tuple)
            or len(history_key) != 2
            or not all(isinstance(part, str) for part in history_key)
        ):
            raise ValueError(f"invalid URL-history key: {history_key}")
        domain, urlkey = history_key
        if not captures:
            raise ValueError(f"URL history is empty: {history_key}")
        domains = {
            domain_folder(capture.original)
            for capture in captures
        }
        if len(domains) != 1:
            raise ValueError(
                f"URL history spans multiple domains: {history_key}"
            )
        if domain not in domains:
            raise ValueError(
                f"URL-history domain differs from URLs: {history_key}"
            )
        original_url = getattr(captures[0], "original", None)
        if not isinstance(original_url, str):
            raise ValueError("capture URL must be a string")
        path = preferred_warc_path(urlkey, original_url, layout)
        key = _equivalent_path(path, layout.collection_root)
        relative = path.relative_to(layout.collection_root).as_posix()
        candidates.setdefault(key, []).append(
            (relative, path, history_key)
        )

    exact = {
        key: (
            min(entries, key=lambda entry: entry[0])[1],
            [entry[2] for entry in entries],
        )
        for key, entries in candidates.items()
    }

    allocated: dict[tuple[str, ...], list[object]] = {}
    for key, (_path, group_keys) in exact.items():
        owner = next(
            (
                key[:length]
                for length in range(1, len(key))
                if key[:length] in exact
            ),
            key,
        )
        allocated.setdefault(owner, []).extend(group_keys)

    return {
        exact[key][0]: tuple(sorted(group_keys))
        for key, group_keys in sorted(
            allocated.items(),
            key=lambda item: exact[item[0]][0]
            .relative_to(layout.collection_root)
            .as_posix(),
        )
    }


def _newest_wins_website_paths(
    planned: list[tuple[Path, UrlHistoryKey, int, str, datetime]],
    layout: CollectionPaths,
) -> list[tuple[Path, UrlHistoryKey, int, str]]:
    """Keep the newest capture per filesystem-equivalent website path.

    Older timestamps at the same path are dropped. Captures that share both
    path and newest timestamp remain for collision handling.
    """

    by_key: dict[
        tuple[str, ...],
        list[tuple[Path, UrlHistoryKey, int, str, datetime]],
    ] = {}
    for entry in planned:
        key = _equivalent_path(entry[0], layout.website_root)
        by_key.setdefault(key, []).append(entry)

    survivors: list[tuple[Path, UrlHistoryKey, int, str]] = []
    for entries in by_key.values():
        newest_timestamp = max(entry[4] for entry in entries)
        survivors.extend(
            entry[:4]
            for entry in entries
            if entry[4] == newest_timestamp
        )
    return survivors


def _disambiguate_website_paths(
    planned: list[tuple[Path, UrlHistoryKey, int, str]],
    layout: CollectionPaths,
) -> list[tuple[Path, UrlHistoryKey, int, str]]:
    """Give colliding website paths distinct digest-suffixed filenames."""

    by_key: dict[
        tuple[str, ...],
        list[tuple[Path, UrlHistoryKey, int, str]],
    ] = {}
    for entry in planned:
        key = _equivalent_path(entry[0], layout.website_root)
        by_key.setdefault(key, []).append(entry)

    disambiguated: list[tuple[Path, UrlHistoryKey, int, str]] = []
    for entries in by_key.values():
        if len(entries) == 1:
            disambiguated.append(entries[0])
            continue

        tokens = {token for _path, _urlkey, _index, token in entries}
        if len(tokens) != len(entries):
            spellings = sorted(
                {
                    path.relative_to(layout.website_root).as_posix()
                    for path, _, _, _ in entries
                }
            )
            raise FileExistsError(
                "multiple captures map to the same website path with "
                "identical digests: " + ", ".join(spellings)
            )

        for path, urlkey, capture_index, token in entries:
            disambiguated.append(
                (
                    _with_filename_token(path, token),
                    urlkey,
                    capture_index,
                    token,
                )
            )
    return disambiguated


def _reshape_website_paths(
    planned: list[tuple[Path, UrlHistoryKey, int, str]],
    layout: CollectionPaths,
) -> list[tuple[Path, UrlHistoryKey, int, str]]:
    """Resolve file-vs-directory conflicts by reshaping files to index.html."""

    # Repeat until stable: reshaping one file can expose another conflict.
    while True:
        by_key: dict[
            tuple[str, ...],
            list[tuple[Path, UrlHistoryKey, int, str]],
        ] = {}
        for entry in planned:
            key = _equivalent_path(entry[0], layout.website_root)
            by_key.setdefault(key, []).append(entry)

        for key, entries in by_key.items():
            if len(entries) > 1:
                spellings = sorted(
                    {
                        path.relative_to(layout.website_root).as_posix()
                        for path, _, _, _ in entries
                    }
                )
                raise FileExistsError(
                    "multiple captures map to the same website path: "
                    + ", ".join(spellings)
                )

        keys = sorted(by_key, key=len)
        strict_prefixes = {
            key[:length]
            for key in keys
            for length in range(1, len(key))
        }
        reshaped: list[tuple[Path, UrlHistoryKey, int, str]] = []
        changed = False
        for key, entries in ((key, by_key[key]) for key in keys):
            path, urlkey, capture_index, token = entries[0]
            if key not in strict_prefixes:
                reshaped.append((path, urlkey, capture_index, token))
                continue

            reshaped_path = path / "index.html"
            conflict_key = _equivalent_path(reshaped_path, layout.website_root)
            if conflict_key in by_key:
                raise FileExistsError(
                    "cannot reshape website file to directory without "
                    f"clobbering: {reshaped_path}"
                )
            reshaped.append((reshaped_path, urlkey, capture_index, token))
            changed = True

        planned = reshaped
        if not changed:
            return planned


def prepare_website_files(
    captures_by_url: Mapping[UrlHistoryKey, Sequence[object]],
    layout: CollectionPaths,
    *,
    include_timestamps: bool,
) -> WebsiteFiles:
    """Plan and inspect all final loose-file targets under ``website/``."""

    planned: list[tuple[Path, UrlHistoryKey, int, str, datetime]] = []
    for history_key, captures in captures_by_url.items():
        if not captures:
            raise ValueError(f"URL history is empty: {history_key}")
        for capture_index, capture in enumerate(captures):
            original = getattr(capture, "original", None)
            if not isinstance(original, str) or not original:
                raise ValueError("capture URL must be a non-empty string")
            capture_timestamp = getattr(capture, "timestamp", None)
            if not isinstance(capture_timestamp, datetime):
                raise ValueError("capture timestamp must be a datetime")
            path_timestamp = capture_timestamp if include_timestamps else None
            path = preferred_website_path(
                original,
                layout,
                mimetype=getattr(capture, "mimetype", None),
                timestamp=path_timestamp,
            )
            planned.append(
                (
                    path,
                    history_key,
                    capture_index,
                    _digest_path_token(
                        getattr(capture, "digest", None),
                        capture_index=capture_index,
                    ),
                    capture_timestamp,
                )
            )

    planned = _newest_wins_website_paths(planned, layout)
    planned = _disambiguate_website_paths(planned, layout)
    planned = _reshape_website_paths(planned, layout)
    planned.sort(
        key=lambda item: (
            item[0].relative_to(layout.website_root).as_posix(),
            item[1],
            item[2],
        )
    )

    targets = []
    for path, history_key, capture_index, _token in planned:
        _inspect_target(path)
        targets.append(
            WebsiteFile(
                path=path,
                capture=captures_by_url[history_key][capture_index],
            )
        )
    return WebsiteFiles(layout, tuple(targets))
