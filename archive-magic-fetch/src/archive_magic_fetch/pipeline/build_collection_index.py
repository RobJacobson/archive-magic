"""Build and validate an annual CDXJ index."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from cdxj_indexer.main import CDXJIndexer

from archive_magic_fetch.archive.format import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
    parse_cdxj_line,
    validate_cdxj_against_warcs,
)
from archive_magic_fetch.archive.identity import (
    cdx_payload_digest_token,
    normalize_payload_digest,
)
from archive_magic_fetch.archive.layout import (
    ArchiveLayout,
    index_artifact_from_path,
    list_collection_warcs,
)
from archive_magic_fetch.archive.scan_warcs import StrictWarcReader, WarcReadOptions, WarcScan
from archive_magic_fetch.models import IndexArtifact
from archive_magic_fetch.runtime.manage_archive_files import (
    exclusive_temp_path,
    publish_file_atomically,
    sync_directory,
)

_CDX_DIGEST_FIELD = "archive-magic:cdx-digest"


_CDX_DIGEST_MATCH_FIELD = "archive-magic:cdx-digest-match"


_CDX_STATUS_FIELD = "archive-magic:cdx-status"


_CDX_URLKEY_FIELD = "archive-magic:cdx-urlkey"


def build_collection_index(
    layout: ArchiveLayout,
    collection_id: str,
    *,
    changed_warcs: Sequence[Path] | None = None,
    warc_sizes: Mapping[str, int] | None = None,
) -> IndexArtifact | None:
    """Build and install an index; ``None`` changes forces a full rebuild."""
    with prepare_collection_index(layout, collection_id, changed_warcs=changed_warcs) as prepared:
        return prepared.install(warc_sizes=warc_sizes)


@dataclass
class PreparedIndex:
    """Scanned records awaiting checkpointing, range validation, and installation."""

    layout: ArchiveLayout
    collection_id: str
    temporary: Path | None
    lines: list[str]
    scans: dict[Path, WarcScan]

    def install(self, *, warc_sizes: Mapping[str, int] | None = None) -> IndexArtifact | None:
        index_path = self.layout.collection_index(self.collection_id)
        surviving = list_collection_warcs(self.layout, self.collection_id)
        if self.temporary is None:
            return index_artifact_from_path(self.layout, index_path) if surviving else None
        if not surviving and warc_sizes is None:
            if index_path.exists():
                index_path.unlink()
                sync_directory(index_path.parent)
            return None
        sizes = (dict(warc_sizes) if warc_sizes is not None
                 else {path.name: path.stat().st_size for path in surviving})
        for path in self.scans:
            if path.is_file():
                sizes[path.name] = path.stat().st_size
        validate_cdxj_against_warcs(
            self.layout, self.collection_id, self.lines, warc_sizes=sizes or None,
        )
        self.temporary.write_text("".join(f"{line}\n" for line in self.lines), encoding="utf-8")
        publish_file_atomically(self.temporary, index_path)
        return index_artifact_from_path(self.layout, index_path, capture_count=len(self.lines))


@contextmanager
def prepare_collection_index(
    layout: ArchiveLayout,
    collection_id: str,
    *,
    changed_warcs: Sequence[Path] | None = None,
    read_options: Mapping[Path, WarcReadOptions] | None = None,
) -> Iterator[PreparedIndex]:
    """Scan once and prepare index rows without installing them.

    The annual stage owns recovery decisions and checkpoints before calling
    ``install``. Temporary output is removed even when that work fails.
    """
    collection_id = layout.validate_collection_id(collection_id)
    index_path = layout.collection_index(collection_id)
    warcs = list_collection_warcs(layout, collection_id)
    if not warcs or (changed_warcs is not None and not changed_warcs and index_path.is_file()):
        yield PreparedIndex(layout, collection_id, None, [], {})
        return
    full_rebuild = changed_warcs is None or not index_path.is_file()
    inputs = warcs if full_rebuild else list(changed_warcs)
    collection_dir = layout.collection_dir(collection_id)
    tmp = exclusive_temp_path(collection_dir, suffix=".cdxj.tmp")
    try:
        indexer = _ArchiveMagicCDXJIndexer(
            output=str(tmp), inputs=[str(path) for path in inputs],
            sort=True, records="response,revisit", dir_root=str(collection_dir),
            read_options=read_options,
            year=collection_id if collection_id.isdigit() and len(collection_id) == 4 else None,
        )
        indexer.process_all()
        lines = _read_cdxj_lines(tmp)
        if not full_rebuild:
            changed_names = {path.name for path in inputs}
            retained = [line for line in _read_cdxj_lines(index_path)
                        if parse_cdxj_line(line)[2].get("filename") not in changed_names]
            lines = sorted([*retained, *lines])
        yield PreparedIndex(layout, collection_id, tmp, lines, indexer.scans)
    finally:
        tmp.unlink(missing_ok=True)


class _ArchiveMagicCDXJIndexer(CDXJIndexer):
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

    def __init__(
        self, *args, read_options: Mapping[Path, WarcReadOptions] | None = None,
        year: str | None = None, **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.read_options = read_options or {}
        self.scans: dict[Path, WarcScan] = {}
        self.year = year
        self.reader: StrictWarcReader | None = None

    def process_one(self, input_, output, filename):
        self.input_path = Path(filename)
        try:
            super().process_one(input_, output, filename)
            assert self.reader is not None and self.reader.result is not None
            self.scans[self.input_path] = self.reader.result
        finally:
            if self.reader is not None:
                self.reader.close()
                self.reader = None

    def _create_record_iter(self, input_):
        options = self.read_options.get(self.input_path, WarcReadOptions(year=self.year))
        self.reader = StrictWarcReader(input_, self.input_path, options)
        return self.reader

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
            return record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) != "false"
        if name == _CDX_STATUS_FIELD:
            return record.rec_headers.get_header(CDX_STATUS_HEADER)
        if name == _CDX_URLKEY_FIELD:
            return record.rec_headers.get_header(CDX_URLKEY_HEADER)
        return super().get_field(record, name, it, filename)


def _read_cdxj_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]
