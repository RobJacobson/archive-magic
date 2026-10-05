"""Stream strict record-compressed WARCs and recover an uncheckpointed EOF."""

from __future__ import annotations

import hmac
import zlib
from dataclasses import dataclass
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import BinaryIO

from warcio.archiveiterator import ArchiveIterator
from warcio.exceptions import ArchiveLoadFailed

from archive_magic_fetch.archive.digests import parse_warc_digest
from archive_magic_fetch.archive.inventory_collection import get_warc_identity
from archive_magic_fetch.runtime.manage_archive_files import sync_file

_BLOCK = 64 * 1024


@dataclass(frozen=True)
class WarcScan:
    records: int
    captures: int
    size: int
    repaired: bool


@dataclass(frozen=True)
class WarcReadOptions:
    year: str | None = None
    durable_size: int = 0
    repair_tail: bool = False
    allow_empty: bool = False


class StrictWarcReader:
    """Yield validated records with compressed locators for the CDXJ indexer.

    The decoded spool stays open until the consumer advances. Validation and
    index extraction share its bytes, never decompressing a member twice.
    The caller owns the input stream and any permission to repair its tail.
    """

    def __init__(self, source: BinaryIO, path: Path, options: WarcReadOptions | None = None):
        self.source, self.path = source, path
        self.options = options or WarcReadOptions()
        self.result: WarcScan | None = None
        self._offset = self._length = 0
        self._iterator = self._records()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._iterator)

    def close(self) -> None:
        self._iterator.close()

    def get_record_offset(self) -> int:
        return self._offset

    def get_record_length(self) -> int:
        return self._length

    def _records(self):
        options, path, source = self.options, self.path, self.source
        records = captures = end = 0
        repaired = False
        checkpoint_seen = options.durable_size == 0
        pending = b""
        while True:
            pending = pending or source.read(_BLOCK)
            if not pending:
                break
            start = source.tell() - len(pending)
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            with SpooledTemporaryFile(max_size=_BLOCK) as decoded:
                while not inflater.eof:
                    data = pending or source.read(_BLOCK)
                    pending = b""
                    if not data:
                        if not options.repair_tail or start < options.durable_size:
                            raise ValueError(f"incomplete gzip member: {path} at {start}")
                        repaired = True
                        break
                    try:
                        decoded.write(inflater.decompress(data, _BLOCK))
                    except zlib.error as error:
                        raise ValueError(f"corrupt gzip member: {path} at {start}: {error}") from error
                    pending = inflater.unused_data if inflater.eof else inflater.unconsumed_tail
                if repaired:
                    break
                end = source.tell() - len(pending)
                try:
                    captures += _validate_record(decoded, path, records == 0, options.year)
                    # Validation consumed the payload. Reparse only the headers
                    # from the same spool so indexer digest fallback can read it.
                    decoded.seek(0)
                    record = next(ArchiveIterator(decoded, verify_http=True))
                except ArchiveLoadFailed as error:
                    raise ValueError(f"invalid WARC record: {path}: {error}") from error
                records += 1
                checkpoint_seen |= end == options.durable_size
                self._offset, self._length = start, end - start
                yield record
        if not checkpoint_seen:
            raise ValueError(f"missing or invalid durable WARC boundary: {path}")
        if repaired:
            with path.open("r+b") as stream:
                stream.truncate(end)
            sync_file(path)
        if (not records and not options.allow_empty) or (not captures and not options.allow_empty):
            raise ValueError(f"WARC contains no captures: {path}")
        self.result = WarcScan(records, captures, end, repaired)


def scan_warc(
    path: Path, *, year: str | None = None, durable_size: int = 0,
    repair_tail: bool = False, allow_empty: bool = False,
) -> WarcScan:
    """Validation-only consumer of the shared strict WARC reader."""
    with path.open("rb") as source:
        reader = StrictWarcReader(source, path, WarcReadOptions(year, durable_size, repair_tail, allow_empty))
        try:
            for _ in reader:
                pass
            assert reader.result is not None
            return reader.result
        finally:
            reader.close()


def _validate_record(stream, path, first, year) -> int:
    stream.seek(0)
    if stream.readline() not in (b"WARC/1.0\r\n", b"WARC/1.1\r\n"):
        raise ValueError(f"invalid WARC version: {path}")
    while line := stream.readline():
        if line == b"\r\n":
            break
    else:
        raise ValueError(f"missing WARC header terminator: {path}")
    block_start = stream.tell()
    stream.seek(0)
    records = ArchiveIterator(stream, check_digests="raise", verify_http=True)
    record = next(records)
    headers = record.rec_headers
    for name in ("WARC-Type", "WARC-Date", "WARC-Record-ID", "Content-Length"):
        if sum(key.lower() == name.lower() for key, _ in headers.headers) != 1:
            raise ValueError(f"missing or duplicate {name}: {path}")
    length = headers.get_header("Content-Length")
    if not length.isdigit():
        raise ValueError(f"invalid WARC Content-Length: {path}")
    # Check byte framing separately: parser EOF does not prove declared length.
    position = stream.tell()
    stream.seek(block_start + int(length))
    if stream.read(5) != b"\r\n\r\n":
        raise ValueError(f"invalid WARC block framing: {path}")
    stream.seek(position)
    while record.raw_stream.read(_BLOCK):
        pass
    if next(records, None) is not None:
        raise ValueError(f"multiple WARC records in one gzip member: {path}")
    # warcio deliberately skips block checking for revisits. Verify their HTTP
    # blocks too; their payload digest identifies the referred response body.
    _verify_block_digests(stream, headers, block_start, int(length), path)
    if first and record.rec_type != "warcinfo":
        raise ValueError(f"WARC missing leading warcinfo: {path}")
    if record.rec_type == "warcinfo":
        return 0
    if record.rec_type not in {"response", "revisit"}:
        raise ValueError(f"unsupported stored WARC record: {path}: {record.rec_type}")
    if (headers.get_header("WARC-Truncated")
            or any(key.lower().startswith("warc-segment-") for key, _ in headers.headers)):
        raise ValueError(f"truncated or segmented stored WARC record: {path}")
    identity = get_warc_identity(record)
    if year is not None and identity.timestamp[:4] != year:
        raise ValueError(f"WARC capture outside collection year: {path}")
    if record.http_headers is None:
        raise ValueError(f"{record.rec_type} missing HTTP headers: {path}")
    return 1


def _verify_block_digests(stream, headers, start, length, path) -> None:
    for name, value in headers.headers:
        if name.lower() != "warc-block-digest":
            continue
        try:
            digester, supplied = parse_warc_digest(value)
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, _BLOCK))
                if not chunk:
                    raise ValueError("incomplete block")
                digester.update(chunk)
                remaining -= len(chunk)
            actual = digester.digest()
        except (ValueError, TypeError) as error:
            raise ValueError(f"invalid WARC block digest: {path}") from error
        if not hmac.compare_digest(actual, supplied):
            raise ValueError(f"WARC block digest mismatch: {path}")
