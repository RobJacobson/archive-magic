"""Collection coverage envelope for merge/resume across fetch runs."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .collection_paths import CollectionPaths


COVERAGE_SCHEMA_VERSION = 2
COVERAGE_FILENAME = "collection.json"


@dataclass(frozen=True)
class CollectionCoverage:
    """Durable date and loose-file mode coverage for one collection."""

    url_pattern: str
    date_start: str
    date_end: str
    files_mode: str
    schema_version: int = COVERAGE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "date_end": self.date_end,
            "date_start": self.date_start,
            "files_mode": self.files_mode,
            "schema_version": self.schema_version,
            "url_pattern": self.url_pattern,
        }

    @classmethod
    def from_dict(cls, data: object) -> CollectionCoverage:
        if not isinstance(data, dict):
            raise ValueError("coverage must be a JSON object")
        try:
            url_pattern = data["url_pattern"]
            date_start = data["date_start"]
            date_end = data["date_end"]
            files_mode = data["files_mode"]
            schema_version = data["schema_version"]
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
        if not isinstance(files_mode, str) or not files_mode:
            raise ValueError("coverage files_mode must be a non-empty string")
        if not isinstance(schema_version, int):
            raise ValueError("coverage schema_version must be an integer")
        if schema_version != COVERAGE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported coverage schema_version: {schema_version}"
            )
        return cls(
            url_pattern=url_pattern,
            date_start=date_start,
            date_end=date_end,
            files_mode=files_mode,
            schema_version=COVERAGE_SCHEMA_VERSION,
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


def resolve_prior_coverage(
    layout: CollectionPaths,
) -> Optional[CollectionCoverage]:
    """Load the current collection manifest when present."""

    return load_coverage(layout)


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
    files_mode: str,
    prior: Optional[CollectionCoverage],
) -> MergedSearchWindow:
    """Union prior coverage with the current request."""

    if prior is None:
        return MergedSearchWindow(
            date_start=date_start,
            date_end=date_end,
            prior=None,
            expanded=False,
        )

    if prior.url_pattern != url_pattern:
        raise CoverageModeError(
            "collection coverage url_pattern "
            f"{prior.url_pattern!r} does not match request {url_pattern!r}; "
            "use a separate collection or remove the incompatible manifest"
        )
    if prior.files_mode != files_mode:
        raise CoverageModeError(
            "collection coverage files_mode "
            f"{prior.files_mode!r} does not match request {files_mode!r}; "
            "use a separate collection or remove the incompatible manifest"
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
    files_mode: str,
) -> CollectionCoverage:
    """Build the coverage envelope after a completed search window."""

    return CollectionCoverage(
        url_pattern=url_pattern,
        date_start=date_start,
        date_end=date_end,
        files_mode=files_mode,
    )
