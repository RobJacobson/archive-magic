"""Portable collection CDXJ construction and validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, Sequence

from cdxj_indexer.main import CDXJIndexer
from warcio.archiveiterator import ArchiveIterator

from .collection import (
    ArchiveLayout,
    exclusive_temp_path,
    index_artifact_from_path,
    list_collection_warcs,
    publish_file_atomically,
)
from .models import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    IndexArtifact,
    normalize_original_url,
    normalize_payload_digest,
)


_CDX_DIGEST_FIELD = "archive-magic:cdx-digest"
_CDX_DIGEST_MATCH_FIELD = "archive-magic:cdx-digest-match"


class ArchiveMagicCDXJIndexer(CDXJIndexer):
    """CDXJ indexer that retains IA digest provenance for payload reuse."""

    field_names = {
        **CDXJIndexer.field_names,
        _CDX_DIGEST_FIELD: "cdxDigest",
        _CDX_DIGEST_MATCH_FIELD: "cdxDigestMatch",
    }
    inv_field_names = {value: key for key, value in field_names.items()}
    DEFAULT_FIELDS = [
        *CDXJIndexer.DEFAULT_FIELDS,
        _CDX_DIGEST_FIELD,
        _CDX_DIGEST_MATCH_FIELD,
    ]

    def get_field(self, record, name, it, filename):
        if name == _CDX_DIGEST_FIELD:
            if record.rec_type != "response":
                return None
            return normalize_payload_digest(
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
        return super().get_field(record, name, it, filename)


def index_warc_fragment(
    layout: ArchiveLayout,
    warc_path: Path,
) -> Path:
    """Build a sorted temporary CDXJ fragment for one finalized WARC."""

    collection_dir = warc_path.parent
    collection_dir.mkdir(parents=True, exist_ok=True)
    tmp = exclusive_temp_path(collection_dir, suffix=".fragment.cdxj.tmp")
    ArchiveMagicCDXJIndexer(
        output=str(tmp),
        inputs=[str(warc_path)],
        sort=True,
        records="response,revisit",
        dir_root=str(collection_dir),
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
    lines.sort(
        key=lambda line: (
            line.split(" ", 2)[0],
            line.split(" ", 2)[1] if " " in line else "",
        )
    )
    return lines


def publish_collection_index(
    layout: ArchiveLayout,
    collection_id: str,
) -> Optional[IndexArtifact]:
    """Rebuild the portable collection CDXJ from every finalized WARC."""

    collection_id = layout.validate_collection_id(collection_id)
    collection_warcs = list_collection_warcs(layout, collection_id)
    if not collection_warcs:
        return None

    collection_dir = layout.collection_dir(collection_id)
    index_path = layout.collection_index(collection_id)
    tmp = exclusive_temp_path(collection_dir, suffix=".cdxj.tmp")
    try:
        ArchiveMagicCDXJIndexer(
            output=str(tmp),
            inputs=[str(path) for path in collection_warcs],
            sort=True,
            records="response,revisit",
            dir_root=str(collection_dir),
        ).process_all()
        lines = [
            line for line in tmp.read_text(encoding="utf-8").splitlines() if line
        ]
        validate_cdxj_against_warcs(layout, collection_id, lines)
        validate_collection_revisit_closure(layout, collection_id)
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
) -> None:
    """Ensure every CDXJ locator points at an immutable finalized range."""

    warc_names = {path.name for path in list_collection_warcs(layout, collection_id)}
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
        if Path(filename).name != filename:
            raise ValueError(f"CDXJ filename must be a WARC basename: {filename}")
        if filename not in warc_names:
            raise ValueError(f"CDXJ references foreign WARC: {filename}")
        warc_path = layout.collection_dir(collection_id) / filename
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


def _response_reference_key(
    target_uri: str,
    warc_date: str,
    warc_payload_digest: str,
) -> tuple[str, str, str] | None:
    """Normalize a response reference used by revisit closure checks."""

    digest = normalize_payload_digest(warc_payload_digest)
    if digest is None or not target_uri or not warc_date:
        return None
    return (
        normalize_original_url(target_uri),
        warc_date,
        digest.lower(),
    )


def validate_collection_revisit_closure(
    layout: ArchiveLayout,
    collection_id: str,
) -> None:
    """Ensure revisits resolve backward within one portable collection.

    A revisit may reference only a full response stored in the same collection.
    Forward, cross-collection, and orphan references are rejected. CDXJ locators
    are assumed already checked by ``validate_cdxj_against_warcs``.
    """

    available: set[tuple[str, str, str]] = set()
    # Collect responses then validate revisits in a second full-collection pass
    # so cross-shard Refers-To targets are visible regardless of shard order.
    warcs = list_collection_warcs(layout, collection_id)
    for path in warcs:
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream, check_digests=False):
                if record.rec_type == "response":
                    key = _response_reference_key(
                        record.rec_headers.get_header("WARC-Target-URI") or "",
                        record.rec_headers.get_header("WARC-Date") or "",
                        record.rec_headers.get_header("WARC-Payload-Digest") or "",
                    )
                    if key is not None:
                        available.add(key)
                record.raw_stream.read()

    for path in warcs:
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream, check_digests=False):
                if record.rec_type != "revisit":
                    record.raw_stream.read()
                    continue
                revisit_date = record.rec_headers.get_header("WARC-Date") or ""
                refers_uri = (
                    record.rec_headers.get_header("WARC-Refers-To-Target-URI")
                    or ""
                )
                refers_date = (
                    record.rec_headers.get_header("WARC-Refers-To-Date") or ""
                )
                payload = (
                    record.rec_headers.get_header("WARC-Payload-Digest") or ""
                )
                key = _response_reference_key(refers_uri, refers_date, payload)
                if key is None:
                    raise ValueError(
                        f"collection revisit in {collection_id} is missing a resolvable "
                        f"response reference"
                    )
                if refers_date > revisit_date:
                    raise ValueError(
                        f"collection revisit in {collection_id} has forward reference "
                        f"from {revisit_date} to {refers_date}"
                    )
                if key not in available:
                    raise ValueError(
                        f"collection revisit in {collection_id} has no earlier response "
                        f"for digest {payload}"
                    )
                record.raw_stream.read()


def reconcile_missing_indexes(layout: ArchiveLayout) -> list[str]:
    """Rebuild portable indexes that are missing or older than their WARCs."""

    updated: list[str] = []
    if not layout.collections_root.is_dir():
        return updated
    for collection_dir in sorted(layout.collections_root.iterdir()):
        if not collection_dir.is_dir():
            continue
        collection_id = layout.validate_collection_id(collection_dir.name)
        if _collection_index_is_stale(layout, collection_id):
            publish_collection_index(layout, collection_id)
            updated.append(collection_id)
    return updated


def _collection_index_is_stale(
    layout: ArchiveLayout, collection_id: str
) -> bool:
    warcs = list_collection_warcs(layout, collection_id)
    if not warcs:
        return False
    index = layout.collection_index(collection_id)
    if not index.is_file():
        return True
    known = cdxj_filenames(index)
    names = {path.name for path in warcs}
    if known != names:
        return True
    index_mtime = index.stat().st_mtime_ns
    return any(path.stat().st_mtime_ns > index_mtime for path in warcs)
