"""Loose website-file export under ``website/``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .retrieval import (
    MalformedContentEncodingError,
    TruncatedWaybackResponseError,
    format_playback_failure_summary,
)


@dataclass
class FilesSummary:
    """Aggregate outcomes for one loose-file export operation."""

    selected: int = 0
    written: int = 0
    redirects_omitted: int = 0
    playback_failures: int = 0
    invalid_content_encoding_failures: int = 0
    truncated_response_failures: int = 0
    content_type_mismatches: int = 0

    def add(self, other: FilesSummary) -> None:
        """Accumulate another URL group's outcomes."""

        self.selected += other.selected
        self.written += other.written
        self.redirects_omitted += other.redirects_omitted
        self.playback_failures += other.playback_failures
        self.invalid_content_encoding_failures += (
            other.invalid_content_encoding_failures
        )
        self.truncated_response_failures += (
            other.truncated_response_failures
        )
        self.content_type_mismatches += other.content_type_mismatches

    def record_playback_failure(
        self,
        error: Optional[Exception] = None,
    ) -> None:
        """Count one playback failure and its actionable category."""

        self.playback_failures += 1
        if isinstance(error, MalformedContentEncodingError):
            self.invalid_content_encoding_failures += 1
        elif isinstance(error, TruncatedWaybackResponseError):
            self.truncated_response_failures += 1


def _find_file_blocker(directory: Path) -> Optional[Path]:
    """Return the nearest existing non-directory ancestor, if any."""

    candidate = directory
    while True:
        if candidate.exists() and not candidate.is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        if candidate.exists() and candidate.is_dir():
            return None
        candidate = parent


def _ensure_parent_directory(path: Path) -> None:
    """Create parents, reshaping an existing file into ``index.html`` if needed."""

    while True:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return
        except FileExistsError as error:
            blocker = _find_file_blocker(path.parent)
            if blocker is None:
                raise OSError(
                    f"cannot create website directory for: {path}"
                ) from error
            reshaped = blocker / "index.html"
            if reshaped.exists():
                raise FileExistsError(
                    "cannot reshape website file to directory without "
                    f"clobbering: {reshaped}"
                ) from error
            temporary = blocker.with_name(blocker.name + ".tmp-reshape")
            if temporary.exists():
                raise FileExistsError(
                    f"website reshape temporary exists: {temporary}"
                ) from error
            blocker.rename(temporary)
            blocker.mkdir(parents=True, exist_ok=False)
            temporary.rename(reshaped)


def write_body(path: Path, body: bytes) -> None:
    """Exclusively create one loose file with its complete response body."""

    _ensure_parent_directory(path)
    with path.open("xb") as handle:
        handle.write(body)


def print_files_summary(summary: FilesSummary, *, files_mode: str) -> None:
    """Print the loose-file aggregate summary."""

    if files_mode == "none":
        print("Files: disabled (none)")
        return

    failures = format_playback_failure_summary(
        summary.playback_failures,
        invalid_content_encoding=(
            summary.invalid_content_encoding_failures
        ),
        truncated_response=summary.truncated_response_failures,
    )
    print(
        f"Files: {summary.written} written ({files_mode}); "
        f"{failures}; "
        f"{summary.content_type_mismatches} content-type mismatches; "
        f"{summary.redirects_omitted} redirects omitted"
    )
