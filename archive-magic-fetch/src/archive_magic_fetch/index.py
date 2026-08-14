"""Portable collection CDXJ construction and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping, Optional, Sequence

from cdxj_indexer.main import CDXJIndexer

from .collection import (
    ArchiveLayout,
    exclusive_temp_path,
    index_artifact_from_path,
    list_collection_warcs,
    publish_file_atomically,
)
from .identity import (
    cdx_payload_digest_token,
    normalize_payload_digest,
)
from .models import IndexArtifact
from .protocol import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
)


_CDX_DIGEST_FIELD = "archive-magic:cdx-digest"
_CDX_DIGEST_MATCH_FIELD = "archive-magic:cdx-digest-match"
_CDX_STATUS_FIELD = "archive-magic:cdx-status"
_CDX_URLKEY_FIELD = "archive-magic:cdx-urlkey"


class ArchiveMagicCDXJIndexer(CDXJIndexer):
    """CDXJ indexer that retains IA digest provenance for payload reuse."""

    field_names = {
        **CDXJIndexer.field_names,
        _CDX_DIGEST_FIELD: "cdxDigest",
        _CDX_DIGEST_MATCH_FIELD: "cdxDigestMatch",
        _CDX_STATUS_FIELD: "cdxStatus",
        _CDX_URLKEY_FIELD: "cdxUrlkey",
    }
    inv_field_names = {value: key for key, value in field_names.items()}
    DEFAULT_FIELDS = [
        *CDXJIndexer.DEFAULT_FIELDS,
        _CDX_DIGEST_FIELD,
        _CDX_DIGEST_MATCH_FIELD,
        _CDX_STATUS_FIELD,
        _CDX_URLKEY_FIELD,
    ]

    def get_field(self, record, name, it, filename):
        if name == _CDX_DIGEST_FIELD:
            if record.rec_type not in {"response", "revisit"}:
                return None
            return cdx_payload_digest_token(
                record.rec_headers.get_header(CDX_PAYLOAD_DIGEST_HEADER)
            )
        if name == _CDX_DIGEST_MATCH_FIELD:
            if record.rec_type != "response":
                return None
            digest = normalize_payload_digest(
                record.rec_headers.get_header(CDX_PAYLOAD_DIGEST_HEADER)
            )
            if digest is None:
                return None
            return (
                record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) != "false"
            )
        if name == _CDX_STATUS_FIELD:
            return record.rec_headers.get_header(CDX_STATUS_HEADER)
        if name == _CDX_URLKEY_FIELD:
            return record.rec_headers.get_header(CDX_URLKEY_HEADER)
        return super().get_field(record, name, it, filename)


def cdxj_filenames(path: Path) -> set[str]:
    """Return every filename field referenced by a CDXJ file."""

    if not path.is_file():
        return set()
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        _, _, meta = parse_cdxj_line(line)
        filename = meta.get("filename")
        if isinstance(filename, str):
            names.add(filename)
    return names


def parse_cdxj_line(line: str) -> tuple[str, str, dict[str, object]]:
    """Parse one CDXJ line into its key, timestamp, and metadata."""

    parts = line.split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"malformed CDXJ line: {line!r}")
    try:
        metadata = json.loads(parts[2])
    except json.JSONDecodeError as error:
        raise ValueError(f"malformed CDXJ metadata: {line!r}") from error
    if not isinstance(metadata, dict):
        raise ValueError(f"CDXJ metadata must be an object: {line!r}")
    return parts[0], parts[1], metadata


def publish_collection_index(
    layout: ArchiveLayout,
    collection_id: str,
    *,
    changed_warcs: Sequence[Path] | None = None,
    warc_sizes: Mapping[str, int] | None = None,
) -> Optional[IndexArtifact]:
    """Publish a complete CDXJ while indexing only changed WARCs when possible."""

    collection_id = layout.validate_collection_id(collection_id)
    index_path = layout.collection_index(collection_id)
    full_rebuild = changed_warcs is None or not index_path.is_file()
    inputs = (
        list_collection_warcs(layout, collection_id)
        if full_rebuild
        else list(changed_warcs)
    )
    if not inputs and not index_path.is_file():
        return None

    collection_dir = layout.collection_dir(collection_id)
    tmp = exclusive_temp_path(collection_dir, suffix=".cdxj.tmp")
    try:
        replacement_lines: list[str] = []
        if inputs:
            ArchiveMagicCDXJIndexer(
                output=str(tmp),
                inputs=[str(path) for path in inputs],
                sort=True,
                records="response,revisit",
                dir_root=str(collection_dir),
            ).process_all()
            replacement_lines = _read_cdxj_lines(tmp)

        if full_rebuild:
            lines = replacement_lines
        else:
            changed_names = {path.name for path in inputs}
            retained = [
                line
                for line in _read_cdxj_lines(index_path)
                if parse_cdxj_line(line)[2].get("filename") not in changed_names
            ]
            lines = sorted([*retained, *replacement_lines])

        sizes = (
            {
                path.name: path.stat().st_size
                for path in list_collection_warcs(layout, collection_id)
            }
            if warc_sizes is None
            else dict(warc_sizes)
        )
        for path in inputs:
            sizes[path.name] = path.stat().st_size
        validate_cdxj_against_warcs(
            layout,
            collection_id,
            lines,
            warc_sizes=sizes or None,
        )
        tmp.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        publish_file_atomically(tmp, index_path)
        return index_artifact_from_path(
            layout,
            index_path,
            capture_count=len(lines),
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def validate_cdxj_against_warcs(
    layout: ArchiveLayout,
    collection_id: str,
    lines: Sequence[str],
    *,
    warc_sizes: Mapping[str, int] | None = None,
) -> None:
    """Ensure every CDXJ locator points at an immutable finalized range."""

    sizes = dict(warc_sizes or {})
    if not sizes:
        sizes = {
            path.name: path.stat().st_size
            for path in list_collection_warcs(layout, collection_id)
        }
    warc_names = set(sizes)
    for line in lines:
        _, _, meta = parse_cdxj_line(line)
        filename = meta.get("filename")
        offset = meta.get("offset")
        length = meta.get("length")
        if not isinstance(filename, str):
            raise ValueError("CDXJ entry missing filename")
        if Path(filename).name != filename:
            raise ValueError(f"CDXJ filename must be a WARC basename: {filename}")
        if filename not in warc_names:
            raise ValueError(f"CDXJ references foreign WARC: {filename}")
        size = sizes[filename]
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

def _read_cdxj_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]

def reconcile_missing_indexes(layout: ArchiveLayout) -> list[str]:
    """Rebuild missing indexes; incrementally replace lines for new or newer WARCs."""

    updated: list[str] = []
    if not layout.collections_root.is_dir():
        return updated
    for collection_dir in sorted(layout.collections_root.iterdir()):
        if not collection_dir.is_dir():
            continue
        collection_id = layout.validate_collection_id(collection_dir.name)
        warcs = list_collection_warcs(layout, collection_id)
        if not warcs:
            continue
        index = layout.collection_index(collection_id)
        if not index.is_file():
            publish_collection_index(layout, collection_id)
            updated.append(collection_id)
            continue
        known = cdxj_filenames(index)
        names = {path.name for path in warcs}
        if known - names:
            publish_collection_index(layout, collection_id)
            updated.append(collection_id)
            continue
        index_mtime = index.stat().st_mtime_ns
        changed = [
            path
            for path in warcs
            if path.name not in known or path.stat().st_mtime_ns > index_mtime
        ]
        if not changed:
            continue
        publish_collection_index(layout, collection_id, changed_warcs=changed)
        updated.append(collection_id)
    return updated
