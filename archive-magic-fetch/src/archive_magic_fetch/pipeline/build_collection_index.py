"""Build and validate an annual CDXJ index."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

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
    read_options: Mapping[Path, WarcReadOptions] | None = None,
    on_scan: Callable[[Path, WarcScan], None] | None = None,
    before_install: Callable[[], None] | None = None,
) -> Optional[IndexArtifact]:
    """Build or reuse CDXJ through the strict local WARC reader.

    ``changed_warcs=None`` forces a full rebuild. Recovery options and scan
    callbacks are supplied by the annual stage; ordinary indexing cannot
    repair files. ``before_install`` durably checkpoints recovered bytes
    before range validation and index installation.
    """

    collection_id = layout.validate_collection_id(collection_id)
    index_path = layout.collection_index(collection_id)
    warcs = list_collection_warcs(layout, collection_id)
    if not warcs:
        return None
    if changed_warcs is not None and not changed_warcs and index_path.is_file():
        return index_artifact_from_path(layout, index_path)
    full_rebuild = changed_warcs is None or not index_path.is_file()
    inputs = warcs if full_rebuild else list(changed_warcs)

    collection_dir = layout.collection_dir(collection_id)
    tmp = exclusive_temp_path(collection_dir, suffix=".cdxj.tmp")
    try:
        replacement_lines: list[str] = []
        if inputs:
            _ArchiveMagicCDXJIndexer(
                output=str(tmp),
                inputs=[str(path) for path in inputs],
                sort=True,
                records="response,revisit",
                dir_root=str(collection_dir),
                read_options=read_options,
                on_scan=on_scan,
                year=collection_id if collection_id.isdigit() and len(collection_id) == 4 else None,
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

        # Recovery may have removed an eligible empty final shard.
        surviving = list_collection_warcs(layout, collection_id)
        if before_install is not None:
            before_install()
        if not surviving and warc_sizes is None:
            index_path.unlink(missing_ok=True)
            sync_directory(collection_dir)
            return None
        sizes = (
            {path.name: path.stat().st_size for path in surviving}
            if warc_sizes is None
            else dict(warc_sizes)
        )
        for path in inputs:
            if path.is_file():
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
        on_scan: Callable[[Path, WarcScan], None] | None = None,
        year: str | None = None, **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.read_options = read_options or {}
        self.on_scan = on_scan
        self.year = year
        self.reader: StrictWarcReader | None = None

    def process_one(self, input_, output, filename):
        self.input_path = Path(filename)
        try:
            super().process_one(input_, output, filename)
            assert self.reader is not None and self.reader.result is not None
            if self.on_scan is not None:
                self.on_scan(self.input_path, self.reader.result)
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
