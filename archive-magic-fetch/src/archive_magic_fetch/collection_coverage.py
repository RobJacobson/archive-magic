"""Collection coverage envelope for merge/resume across fetch runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .collection_paths import CollectionPaths


COVERAGE_SCHEMA_VERSION = 1
COVERAGE_FILENAME = "collection.json"


@dataclass(frozen=True)
class CollectionCoverage:
    """Durable date and mode coverage for one collection.

    When ``modes_confirmed`` is False (bootstrapped from source query
    snapshots that omit modes), only the date envelope is trusted.
    """

    url_pattern: str
    date_start: str
    date_end: str
    warc_mode: str
    files_mode: str
    redirect_capture: str
    schema_version: int = COVERAGE_SCHEMA_VERSION
    modes_confirmed: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "date_end": self.date_end,
            "date_start": self.date_start,
            "files_mode": self.files_mode,
            "redirect_capture": self.redirect_capture,
            "schema_version": self.schema_version,
            "url_pattern": self.url_pattern,
            "warc_mode": self.warc_mode,
        }

    @classmethod
    def from_dict(cls, data: object) -> CollectionCoverage:
        if not isinstance(data, dict):
            raise ValueError("coverage must be a JSON object")
        try:
            url_pattern = data["url_pattern"]
            date_start = data["date_start"]
            date_end = data["date_end"]
            warc_mode = data["warc_mode"]
            files_mode = data["files_mode"]
            redirect_capture = data["redirect_capture"]
            schema_version = data.get(
                "schema_version",
                COVERAGE_SCHEMA_VERSION,
            )
        except KeyError as error:
            raise ValueError(
                f"coverage missing field: {error.args[0]}"
            ) from error
        if not isinstance(url_pattern, str) or not url_pattern:
            raise ValueError("coverage url_pattern must be a non-empty string")
        if not isinstance(date_start, str) or not date_start:
            raise ValueError("coverage date_start must be a non-empty string")
        if not isinstance(date_end, str) or not date_end:
            raise ValueError("coverage date_end must be a non-empty string")
        if not isinstance(warc_mode, str) or not warc_mode:
            raise ValueError("coverage warc_mode must be a non-empty string")
        if not isinstance(files_mode, str) or not files_mode:
            raise ValueError("coverage files_mode must be a non-empty string")
        if not isinstance(redirect_capture, str) or not redirect_capture:
            raise ValueError(
                "coverage redirect_capture must be a non-empty string"
            )
        if not isinstance(schema_version, int):
            raise ValueError("coverage schema_version must be an integer")
        return cls(
            url_pattern=url_pattern,
            date_start=date_start,
            date_end=date_end,
            warc_mode=warc_mode,
            files_mode=files_mode,
            redirect_capture=redirect_capture,
            schema_version=schema_version,
            modes_confirmed=True,
        )


def coverage_path(layout: CollectionPaths) -> Path:
    """Return the collection coverage manifest path."""

    return layout.collection_root / COVERAGE_FILENAME


def _start_key(value: str) -> str:
    return value.ljust(14, "0")


def _end_key(value: str) -> str:
    return value.ljust(14, "9")


def earlier_date(left: str, right: str) -> str:
    """Return the earlier CDX date bound (more inclusive start)."""

    return left if _start_key(left) <= _start_key(right) else right


def later_date(left: str, right: str) -> str:
    """Return the later CDX date bound (more inclusive end)."""

    return left if _end_key(left) >= _end_key(right) else right


def load_coverage(layout: CollectionPaths) -> Optional[CollectionCoverage]:
    """Load collection.json when present and valid."""

    path = coverage_path(layout)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CollectionCoverage.from_dict(data)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"cannot read collection coverage {path}: {error}"
        ) from error


def _read_source_query(path: Path) -> Optional[dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def bootstrap_coverage(
    layout: CollectionPaths,
    *,
    url_pattern: str,
) -> Optional[CollectionCoverage]:
    """Infer coverage from matching sources/*/query.json when missing."""

    sources_root = layout.sources_root
    if not sources_root.is_dir():
        return None

    starts: list[str] = []
    ends: list[str] = []

    for entry in sorted(sources_root.iterdir()):
        if not entry.is_dir():
            continue
        query_path = entry / "query.json"
        if not query_path.is_file():
            continue
        data = _read_source_query(query_path)
        if data is None:
            continue
        pattern = data.get("url_pattern")
        date_start = data.get("date_start")
        date_end = data.get("date_end")
        if pattern != url_pattern:
            continue
        if not isinstance(date_start, str) or not date_start:
            continue
        if not isinstance(date_end, str) or not date_end:
            continue
        starts.append(date_start)
        ends.append(date_end)

    if not starts:
        return None

    date_start = starts[0]
    for value in starts[1:]:
        date_start = earlier_date(date_start, value)
    date_end = ends[0]
    for value in ends[1:]:
        date_end = later_date(date_end, value)

    return CollectionCoverage(
        url_pattern=url_pattern,
        date_start=date_start,
        date_end=date_end,
        warc_mode="all",
        files_mode="none",
        redirect_capture="page",
        modes_confirmed=False,
    )


def resolve_prior_coverage(
    layout: CollectionPaths,
    *,
    url_pattern: str,
) -> Optional[CollectionCoverage]:
    """Load collection.json, else bootstrap from source query files."""

    prior = load_coverage(layout)
    if prior is not None:
        return prior
    return bootstrap_coverage(layout, url_pattern=url_pattern)


def save_coverage(
    layout: CollectionPaths,
    coverage: CollectionCoverage,
) -> Path:
    """Atomically publish updated collection coverage."""

    path = coverage_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            coverage.to_dict(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".collection-",
        suffix=".json.tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class MergedSearchWindow:
    """Requested vs effective CDX date window after coverage merge."""

    date_start: str
    date_end: str
    prior: Optional[CollectionCoverage]
    expanded: bool


class CoverageModeError(ValueError):
    """Raised when a merge would mix incompatible output modes."""


def merge_search_window(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    warc_mode: str,
    files_mode: str,
    redirect_capture: str,
    prior: Optional[CollectionCoverage],
    fresh: bool,
) -> MergedSearchWindow:
    """Union prior coverage with the current request, or use the request alone."""

    if fresh or prior is None:
        return MergedSearchWindow(
            date_start=date_start,
            date_end=date_end,
            prior=None if fresh else prior,
            expanded=False,
        )

    if prior.url_pattern != url_pattern:
        raise CoverageModeError(
            "collection coverage url_pattern "
            f"{prior.url_pattern!r} does not match request {url_pattern!r}; "
            "use --fresh to ignore prior coverage"
        )
    if prior.modes_confirmed:
        if prior.warc_mode != warc_mode:
            raise CoverageModeError(
                "collection coverage warc_mode "
                f"{prior.warc_mode!r} does not match request {warc_mode!r}; "
                "use --fresh to ignore prior coverage"
            )
        if prior.files_mode != files_mode:
            raise CoverageModeError(
                "collection coverage files_mode "
                f"{prior.files_mode!r} does not match request {files_mode!r}; "
                "use --fresh to ignore prior coverage"
            )
        if prior.redirect_capture != redirect_capture:
            raise CoverageModeError(
                "collection coverage redirect_capture "
                f"{prior.redirect_capture!r} does not match request "
                f"{redirect_capture!r}; use --fresh to ignore prior coverage"
            )

    merged_start = earlier_date(prior.date_start, date_start)
    merged_end = later_date(prior.date_end, date_end)
    expanded = (
        merged_start != date_start
        or merged_end != date_end
    )
    return MergedSearchWindow(
        date_start=merged_start,
        date_end=merged_end,
        prior=prior,
        expanded=expanded,
    )


def coverage_after_run(
    *,
    url_pattern: str,
    date_start: str,
    date_end: str,
    warc_mode: str,
    files_mode: str,
    redirect_capture: str,
) -> CollectionCoverage:
    """Build the coverage envelope after a completed search window."""

    return CollectionCoverage(
        url_pattern=url_pattern,
        date_start=date_start,
        date_end=date_end,
        warc_mode=warc_mode,
        files_mode=files_mode,
        redirect_capture=redirect_capture,
    )
