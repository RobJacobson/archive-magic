"""Optional post-write rewrite of website links for local browsing."""

from __future__ import annotations

import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional
from urllib.parse import urlsplit

from .collection_paths import domain_folder, preferred_site_file


_REWRITE_EXTENSIONS = {".html", ".htm", ".css", ".js"}
_HTML_EXTENSIONS = {".html", ".htm"}
_TIMESTAMP_DIR = re.compile(r"^\d{14}$")
_ATTR_RE = re.compile(
    r'(?P<prefix>\b(?:href|src|action)\s*=\s*)'
    r'(?P<quote>["\'])(?P<url>.*?)(?P=quote)',
    re.IGNORECASE,
)
_SRCSET_RE = re.compile(
    r'(?P<prefix>\bsrcset\s*=\s*)'
    r'(?P<quote>["\'])(?P<value>.*?)(?P=quote)',
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(
    r'url\(\s*(?P<quote>["\']?)(?P<url>.*?)(?P=quote)\s*\)',
    re.IGNORECASE,
)
_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")


@dataclass
class RewriteSummary:
    """Aggregate outcomes for one local-link rewrite pass."""

    rewritten: int = 0
    skipped_decode: int = 0
    unchanged: int = 0


def _known_host_segments(website_root: Path) -> set[str]:
    if not website_root.is_dir():
        return set()
    return {
        entry.name
        for entry in website_root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    }


def _site_context(
    website_root: Path,
    file_path: Path,
    *,
    include_timestamps: bool,
) -> tuple[str, Path]:
    """Return host segment and site root for one file under website/."""

    relative = file_path.relative_to(website_root)
    if not relative.parts:
        raise ValueError(f"file is not under website root: {file_path}")
    host = relative.parts[0]
    if (
        include_timestamps
        and len(relative.parts) >= 2
        and _TIMESTAMP_DIR.fullmatch(relative.parts[1])
    ):
        return host, website_root / host / relative.parts[1]
    return host, website_root / host


def _existing_local_target(
    site_root: Path,
    url_path: str,
) -> Optional[Path]:
    candidate = preferred_site_file(site_root, url_path)
    if candidate.is_file():
        return candidate
    if candidate.name != "index.html":
        as_directory_index = candidate / "index.html"
        if as_directory_index.is_file():
            return as_directory_index
    parent = preferred_site_file(site_root, url_path.rstrip("/") + "/")
    if parent != candidate and parent.is_file():
        return parent
    return None


def _relative_link(
    current_file: Path,
    target: Path,
    website_root: Path,
    *,
    fragment: str,
) -> str:
    current = PurePosixPath(
        *current_file.relative_to(website_root).parts
    )
    destination = PurePosixPath(*target.relative_to(website_root).parts)
    relative = posixpath.relpath(
        destination.as_posix(),
        start=current.parent.as_posix() or ".",
    )
    if fragment:
        return f"{relative}#{fragment}"
    return relative


def _host_segment_for_url(url: str, known_hosts: set[str]) -> Optional[str]:
    try:
        host = domain_folder(url)
    except ValueError:
        return None
    if host in known_hosts:
        return host
    return None


def rewrite_reference(
    reference: str,
    *,
    current_file: Path,
    website_root: Path,
    known_hosts: set[str],
    include_timestamps: bool = False,
) -> str:
    """Rewrite one URL reference when a same-collection local target exists."""

    raw = reference.strip()
    if not raw or raw.startswith("#"):
        return reference

    lowered = raw.lower()
    if lowered.startswith(_SKIP_SCHEMES):
        return reference

    if raw.startswith("//"):
        absolute = f"https:{raw}"
        parsed = urlsplit(absolute)
    else:
        parsed = urlsplit(raw)
        if parsed.scheme:
            if parsed.scheme.lower() not in {"http", "https"}:
                return reference
            absolute = raw
        elif raw.startswith("/"):
            host_segment, site_root = _site_context(
                website_root,
                current_file,
                include_timestamps=include_timestamps,
            )
            target = _existing_local_target(
                site_root,
                parsed.path or "/",
            )
            if target is None:
                return reference
            return _relative_link(
                current_file,
                target,
                website_root,
                fragment=parsed.fragment,
            )
        else:
            # Already relative (no scheme, not root- or scheme-relative).
            return reference

    host_segment = _host_segment_for_url(absolute, known_hosts)
    if host_segment is None:
        return reference

    _current_host, site_root = _site_context(
        website_root,
        current_file,
        include_timestamps=include_timestamps,
    )
    if host_segment != _current_host:
        # Absolute same-collection host that is not this file's host tree:
        # resolve under that host's non-timestamp root.
        site_root = website_root / host_segment

    target = _existing_local_target(
        site_root,
        parsed.path or "/",
    )
    if target is None:
        return reference
    return _relative_link(
        current_file,
        target,
        website_root,
        fragment=parsed.fragment,
    )


def _rewrite_srcset(
    value: str,
    *,
    current_file: Path,
    website_root: Path,
    known_hosts: set[str],
    include_timestamps: bool,
) -> str:
    parts = []
    for candidate in value.split(","):
        item = candidate.strip()
        if not item:
            parts.append(candidate)
            continue
        pieces = item.split()
        url = pieces[0]
        rewritten = rewrite_reference(
            url,
            current_file=current_file,
            website_root=website_root,
            known_hosts=known_hosts,
            include_timestamps=include_timestamps,
        )
        if len(pieces) == 1:
            parts.append(rewritten)
        else:
            parts.append(" ".join([rewritten, *pieces[1:]]))
    # Preserve a simple comma-separated form; whitespace around commas is fine.
    return ", ".join(part for part in parts if part != "")


def rewrite_text(
    text: str,
    *,
    current_file: Path,
    website_root: Path,
    known_hosts: set[str],
    include_timestamps: bool = False,
) -> str:
    """Rewrite HTML/CSS/JS URL references in one text document."""

    suffix = current_file.suffix.lower()

    def replace_attr(match: re.Match[str]) -> str:
        rewritten = rewrite_reference(
            match.group("url"),
            current_file=current_file,
            website_root=website_root,
            known_hosts=known_hosts,
            include_timestamps=include_timestamps,
        )
        return (
            f'{match.group("prefix")}{match.group("quote")}'
            f'{rewritten}{match.group("quote")}'
        )

    def replace_srcset(match: re.Match[str]) -> str:
        rewritten = _rewrite_srcset(
            match.group("value"),
            current_file=current_file,
            website_root=website_root,
            known_hosts=known_hosts,
            include_timestamps=include_timestamps,
        )
        return (
            f'{match.group("prefix")}{match.group("quote")}'
            f'{rewritten}{match.group("quote")}'
        )

    def replace_css_url(match: re.Match[str]) -> str:
        rewritten = rewrite_reference(
            match.group("url"),
            current_file=current_file,
            website_root=website_root,
            known_hosts=known_hosts,
            include_timestamps=include_timestamps,
        )
        quote = match.group("quote")
        return f"url({quote}{rewritten}{quote})"

    # HTML attributes/srcset: HTML and JS (DOM-style assignments). Not CSS.
    if suffix in _HTML_EXTENSIONS or suffix == ".js":
        text = _ATTR_RE.sub(replace_attr, text)
        text = _SRCSET_RE.sub(replace_srcset, text)
    # CSS url(): CSS and HTML inline styles only — never .js helpers named url().
    if suffix in _HTML_EXTENSIONS or suffix == ".css":
        text = _CSS_URL_RE.sub(replace_css_url, text)
    return text


def _write_replaced(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp-rewrite")
    try:
        temporary.write_text(text, encoding="utf-8", newline="")
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        raise


def rewrite_local_links(
    website_root: Path,
    *,
    include_timestamps: bool = False,
) -> RewriteSummary:
    """Rewrite HTML/CSS/JS under ``website/`` for local relative browsing."""

    summary = RewriteSummary()
    if not website_root.is_dir():
        return summary

    known_hosts = _known_host_segments(website_root)
    if not known_hosts:
        return summary

    for path in sorted(website_root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in _REWRITE_EXTENSIONS:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print(
                f"WARNING skipped rewrite (decode error): "
                f"{path.relative_to(website_root).as_posix()}",
                file=sys.stderr,
            )
            summary.skipped_decode += 1
            continue

        rewritten = rewrite_text(
            original,
            current_file=path,
            website_root=website_root,
            known_hosts=known_hosts,
            include_timestamps=include_timestamps,
        )
        if rewritten == original:
            summary.unchanged += 1
            continue
        _write_replaced(path, rewritten)
        summary.rewritten += 1

    print(
        f"Local links: {summary.rewritten} rewritten, "
        f"{summary.skipped_decode} skipped"
    )
    return summary
