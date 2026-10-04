"""Strictly validate a gzip member and normalize its Common Crawl response."""

import base64
import hashlib
import hmac
import re
from concurrent.futures import CancelledError
import zlib
from io import BytesIO

from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.adapters.interpret_common_crawl_failures import (
    CorruptRecord, IdentityMismatch, SourceTruncated, UnsupportedRecord,
)
from archive_magic_fetch.archive.format import semantic_headers
from archive_magic_fetch.archive.identity import (
    normalize_original_url, normalize_payload_digest, payload_digest, warc_date_to_cdx,
)
from archive_magic_fetch.models import CaptureRef, CaptureResult


def decode_warc_capture(data: bytes, capture: CaptureRef) -> CaptureResult:
    """Decode one attempt; parser EOF alone never establishes integrity."""
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = inflater.decompress(data) + inflater.flush()
        if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
            raise CorruptRecord("gzip member incomplete or contains trailing data/members")
        if not decoded.startswith((b"WARC/1.0\r\n", b"WARC/1.1\r\n")):
            raise UnsupportedRecord("only WARC 1.0/1.1 response records are supported")
        with BytesIO(decoded) as stream:
            records = ArchiveIterator(stream, verify_http=True)
            record = next(records)
            headers = record.rec_headers
            for name in ("Content-Length", "WARC-Type", "WARC-Date", "WARC-Target-URI"):
                if sum(key.lower() == name.lower() for key, _ in headers.headers) != 1:
                    raise CorruptRecord(f"missing or duplicate {name}")
            declared = headers.get_header("Content-Length")
            if not declared.isdigit():
                raise CorruptRecord("invalid WARC Content-Length")
            # warcio's total_len counts decoded characters, not source bytes.
            block_start = _header_end(decoded)
            block_end = block_start + int(declared)
            if decoded[block_end:] != b"\r\n\r\n":
                raise CorruptRecord("WARC block length/framing mismatch")
            block = decoded[block_start:block_end]
            if len(block) != int(declared):
                raise CorruptRecord("incomplete WARC block")
            if headers.get_header("WARC-Truncated"):
                raise SourceTruncated("source WARC declares truncation")
            if any(key.lower().startswith("warc-segment-") for key, _ in headers.headers):
                raise UnsupportedRecord("segmented source records are unsupported")
            if record.rec_type != "response" or record.http_headers is None:
                raise UnsupportedRecord(f"unsupported source record type: {record.rec_type}")
            body = record.raw_stream.read()
            if body != block[_header_end(block):]:
                raise CorruptRecord("incomplete HTTP block consumption")
            # Compute BOTH checks independently over exact bytes, never relying on
            # warcio's aggregate digest flag or its lenient EOF handling.
            for name, value in headers.headers:
                if name.lower() == "warc-block-digest":
                    _verify_digest(value, block, "block")
                elif name.lower() == "warc-payload-digest":
                    _verify_digest(value, body, "payload")
            if next(records, None) is not None:
                raise CorruptRecord("range contained multiple WARC records")
            identity = capture.identity
            target = headers.get_header("WARC-Target-URI")
            date = headers.get_header("WARC-Date")
            status = record.http_headers.get_statuscode()
            if not re.fullmatch(r"[1-5][0-9]{2}", status):
                raise CorruptRecord("invalid original HTTP status")
            if (normalize_original_url(target) != identity.original_url
                    or warc_date_to_cdx(date) != identity.timestamp
                    or (identity.status_token != "-" and status != identity.status_token)):
                raise IdentityMismatch("source URL, timestamp, or HTTP status differs from index")
            digest = payload_digest(body)
            expected = normalize_payload_digest(identity.payload_digest)
            assert capture.locator is not None
            return CaptureResult(
                identity, body, int(status),
                tuple(semantic_headers(record.http_headers.headers, len(body), status_code=int(status))),
                date, "https://data.commoncrawl.org/" + capture.locator.filename,
                digest, digest_matched=expected is not None and digest == expected,
            )
    except (CancelledError, CorruptRecord, UnsupportedRecord, SourceTruncated, IdentityMismatch):
        raise
    except Exception as error:
        raise CorruptRecord(f"invalid compressed WARC record: {error}") from error


def _header_end(data: bytes) -> int:
    """Locate the raw byte boundary without re-encoding parsed header values."""
    with BytesIO(data) as stream:
        while line := stream.readline():
            if line in (b"\r\n", b"\n"):
                return stream.tell()
    raise CorruptRecord("missing header terminator")


def _verify_digest(value: str, data: bytes, kind: str) -> None:
    algorithm, separator, expected = value.partition(":")
    if not separator:
        raise CorruptRecord(f"invalid {kind} digest")
    try:
        digest = hashlib.new(algorithm.lower(), data).digest()
        if len(expected) == len(digest) * 2:
            supplied = bytes.fromhex(expected)
        elif len(expected) == len(base64.b32encode(digest)):
            supplied = base64.b32decode(expected.upper())
        else:
            supplied = base64.b64decode(expected, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise CorruptRecord(f"unsupported or invalid {kind} digest") from error
    if not hmac.compare_digest(digest, supplied):
        raise CorruptRecord(f"WARC {kind} digest mismatch")
