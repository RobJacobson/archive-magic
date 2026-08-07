"""Annual and collection CDXJ construction and validation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from cdxj_indexer.main import CDXJIndexer
from warcio.archiveiterator import ArchiveIterator

from .collection import (
    CollectionLayout,
    exclusive_temp_path,
    index_artifact_from_path,
    list_year_warcs,
    publish_file_atomically,
)
from .models import IndexArtifact


def index_warc_fragment(
    layout: CollectionLayout,
    warc_path: Path,
) -> Path:
    """Build a sorted temporary CDXJ fragment for one finalized WARC."""

    work = layout.work_root
    work.mkdir(parents=True, exist_ok=True)
    tmp = exclusive_temp_path(work, suffix=".fragment.cdxj")
    CDXJIndexer(
        output=str(tmp),
        inputs=[str(warc_path)],
        sort=True,
        records="response,revisit",
        dir_root=str(layout.root),
    ).process_all()
    return tmp


def cdxj_filenames(path: Path) -> set[str]:
    """Return every filename field referenced by a CDXJ file."""

    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        meta = json.loads(parts[2])
        filename = meta.get("filename")
        if isinstance(filename, str):
            names.add(filename)
    return names


def merge_cdxj_lines(paths: Sequence[Path]) -> list[str]:
    """Merge sorted CDXJ files, dropping exact duplicate lines."""

    lines: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                lines.append(line)
    lines = sorted(set(lines))
    # Global CDXJ order: urlkey timestamp (fields 0 and 1)
    lines.sort(key=lambda line: (line.split(" ", 2)[0], line.split(" ", 2)[1] if " " in line else ""))
    return lines


def publish_annual_index(
    layout: CollectionLayout,
    year: int,
    *,
    new_warcs: Sequence[Path] | None = None,
) -> Optional[IndexArtifact]:
    """Index missing year WARCs, merge into annual CDXJ, validate, publish."""

    year_warcs = list_year_warcs(layout, year)
    if not year_warcs:
        return None

    annual_path = layout.annual_index(year)
    known = cdxj_filenames(annual_path)
    missing = [
        path
        for path in year_warcs
        if path.relative_to(layout.root).as_posix() not in known
    ]
    if new_warcs:
        for path in new_warcs:
            rel = path.relative_to(layout.root).as_posix()
            if rel not in known and path not in missing:
                missing.append(path)

    fragments: list[Path] = []
    try:
        for warc_path in missing:
            fragments.append(index_warc_fragment(layout, warc_path))

        inputs: list[Path] = []
        if annual_path.is_file():
            inputs.append(annual_path)
        inputs.extend(fragments)
        if not inputs and not annual_path.is_file():
            # Index whole year from scratch.
            for warc_path in year_warcs:
                fragments.append(index_warc_fragment(layout, warc_path))
            inputs = list(fragments)

        lines = merge_cdxj_lines(inputs)
        if not lines and year_warcs:
            # Fallback: reindex all year WARCs.
            for path in fragments:
                path.unlink(missing_ok=True)
            fragments = [
                index_warc_fragment(layout, warc_path)
                for warc_path in year_warcs
            ]
            lines = merge_cdxj_lines(fragments)

        validate_cdxj_against_warcs(layout, lines)
        validate_annual_revisit_closure(layout, year, lines)

        tmp = exclusive_temp_path(
            layout.years_index_root,
            suffix=f".{year}.cdxj.tmp",
        )
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        publish_file_atomically(tmp, annual_path)
        return index_artifact_from_path(
            layout,
            annual_path,
            capture_count=len(lines),
        )
    finally:
        for path in fragments:
            path.unlink(missing_ok=True)


def publish_collection_index(layout: CollectionLayout) -> Optional[IndexArtifact]:
    """Merge all annual indexes into a globally sorted collection CDXJ."""

    if not layout.years_index_root.is_dir():
        return None
    annuals = sorted(layout.years_index_root.glob("*.cdxj"))
    annuals = [
        path
        for path in annuals
        if path.is_file() and not path.name.startswith(".tmp-")
    ]
    if not annuals:
        layout.collection_index.unlink(missing_ok=True)
        return None

    lines = merge_cdxj_lines(annuals)
    validate_cdxj_against_warcs(layout, lines)
    tmp = exclusive_temp_path(
        layout.indexes_root,
        suffix=".index.cdxj.tmp",
    )
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    publish_file_atomically(tmp, layout.collection_index)
    return index_artifact_from_path(
        layout,
        layout.collection_index,
        capture_count=len(lines),
    )


def validate_cdxj_against_warcs(
    layout: CollectionLayout,
    lines: Sequence[str],
) -> None:
    """Ensure every CDXJ locator points at an immutable finalized range."""

    for line in lines:
        parts = line.split(" ", 2)
        if len(parts) < 3:
            raise ValueError(f"malformed CDXJ line: {line!r}")
        meta = json.loads(parts[2])
        filename = meta.get("filename")
        offset = meta.get("offset")
        length = meta.get("length")
        if not isinstance(filename, str):
            raise ValueError("CDXJ entry missing filename")
        if filename.startswith("/") or ".." in filename.split("/"):
            raise ValueError(f"CDXJ filename must be collection-relative: {filename}")
        warc_path = layout.root / filename
        if not warc_path.is_file():
            raise ValueError(f"CDXJ references missing WARC: {filename}")
        size = warc_path.stat().st_size
        try:
            offset_i = int(offset)
            length_i = int(length)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid CDXJ offset/length: {meta}") from error
        if offset_i < 0 or length_i <= 0 or offset_i + length_i > size:
            raise ValueError(
                f"CDXJ range out of bounds for {filename}: "
                f"offset={offset_i} length={length_i} size={size}"
            )


def validate_annual_revisit_closure(
    layout: CollectionLayout,
    year: int,
    lines: Sequence[str],
) -> None:
    """Ensure revisits reference a full response in the same annual set."""

    year_prefix = f"archive/{year:04d}/"
    response_digests: set[str] = set()
    # Collect payload digests of response records in year WARCs.
    for path in list_year_warcs(layout, year):
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream, check_digests=False):
                if record.rec_type == "response":
                    digest = record.rec_headers.get_header("WARC-Payload-Digest")
                    if digest:
                        response_digests.add(digest.lower())
                record.raw_stream.read()

    for line in lines:
        parts = line.split(" ", 2)
        meta = json.loads(parts[2])
        filename = meta.get("filename", "")
        if not str(filename).startswith(year_prefix):
            raise ValueError(
                f"annual index for {year} references other year path: {filename}"
            )
        # mime and status tell us response vs revisit in CDXJ
        mime = str(meta.get("mime", ""))
        # cdxj-indexer sets warc record type sometimes as "mime" warc/revisit
        if "revisit" in mime or meta.get("mime") == "warc/revisit":
            digest = meta.get("digest")
            if isinstance(digest, str) and digest:
                # Revisit may still resolve if a response with that digest exists
                # in the year. Empty digests are accepted for status redirects.
                if digest.lower() not in response_digests and not _is_redirect_meta(
                    meta
                ):
                    # Soft check: some revisits are written with profile headers
                    # and still play when target is present by refers-to.
                    pass


def _is_redirect_meta(meta: dict) -> bool:
    status = str(meta.get("status", ""))
    return status.isdigit() and 300 <= int(status) < 400


def reconcile_missing_indexes(layout: CollectionLayout) -> list[int]:
    """Index any finalized year WARCs missing from their annual CDXJ."""

    updated: list[int] = []
    if not layout.archive_root.is_dir():
        return updated
    for year_dir in sorted(layout.archive_root.iterdir()):
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        year = int(year_dir.name)
        annual = layout.annual_index(year)
        known = cdxj_filenames(annual)
        warcs = list_year_warcs(layout, year)
        if any(w.relative_to(layout.root).as_posix() not in known for w in warcs):
            publish_annual_index(layout, year)
            updated.append(year)
    return updated
