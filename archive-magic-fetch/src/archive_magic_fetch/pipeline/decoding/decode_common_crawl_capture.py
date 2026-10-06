"""Validate Common Crawl records, with digest-verified recovery of two ARC defects."""

import hmac
import ipaddress
import re
from concurrent.futures import CancelledError
import zlib
from io import BytesIO
from urllib.parse import urlsplit

from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.adapters.interpret_common_crawl_failures import (
    CorruptRecord, IdentityMismatch, MalformedArcRecord, MalformedWarcRecord, RecordLimitExceeded,
    SourceTruncated, UnsupportedRecord,
)
from archive_magic_fetch.archive.digests import parse_warc_digest
from archive_magic_fetch.archive.format import semantic_headers
from archive_magic_fetch.archive.identity import (
    cdx_timestamp_to_warc_date, normalize_original_url, normalize_payload_digest,
    payload_digest, warc_date_to_cdx,
)
from archive_magic_fetch.models import CaptureRef, CaptureResult


MAX_DECOMPRESSED_RECORD_BYTES = 64 * 1024 * 1024
MAX_ARC_HEADER_BYTES = 64 * 1024
MAX_WARC_HEADER_BYTES = 64 * 1024
MAX_HTTP_HEADER_BYTES = 64 * 1024


def decode_common_crawl_capture(data: bytes, capture: CaptureRef) -> CaptureResult:
    """Validate transport integrity before attempting any ARC-only recovery."""
    decoded = _inflate_record(data)
    if decoded.startswith(b"WARC/"):
        try:
            return _decode_record(decoded, capture)
        except CorruptRecord as error:
            raise MalformedWarcRecord(f"malformed WARC: {error}") from error
    try:
        return _decode_record(decoded, capture)
    except CorruptRecord:
        try:
            return _recover_arc(decoded, capture)
        except CorruptRecord as error:
            raise MalformedArcRecord(f"malformed ARC: {error}") from error


def _inflate_record(data: bytes) -> bytes:
    """Bound each decompression step, including the one-byte overflow probe."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    output = BytesIO()
    try:
        for offset in range(0, len(data), 65536):
            pending = data[offset:offset + 65536]
            while pending:
                chunk = inflater.decompress(
                    pending, min(65536, MAX_DECOMPRESSED_RECORD_BYTES - output.tell() + 1)
                )
                if output.tell() + len(chunk) > MAX_DECOMPRESSED_RECORD_BYTES:
                    raise RecordLimitExceeded("archive record exceeds decompressed size limit")
                output.write(chunk)
                if inflater.unused_data:
                    raise CorruptRecord("gzip member contains trailing data/members")
                pending = inflater.unconsumed_tail
        if not inflater.eof:
            raise CorruptRecord("gzip member incomplete")
        return output.getvalue()
    except zlib.error as error:
        raise CorruptRecord(f"invalid gzip member: {error}") from error


def _decode_record(decoded: bytes, capture: CaptureRef, *, repairs: tuple[str, ...] = ()) -> CaptureResult:
    """Strict ARC/WARC parsing shared by original bytes and recovery candidates."""
    try:
        if decoded.startswith(b"WARC/"):
            if not decoded.startswith((b"WARC/1.0\r\n", b"WARC/1.1\r\n")):
                raise UnsupportedRecord("unsupported WARC version")
            block_start = _header_end(decoded, max_bytes=MAX_WARC_HEADER_BYTES, kind="WARC")
            arc_frame = None
        else:
            arc_frame = _arc_frame(decoded)
        with BytesIO(decoded) as stream:
            # Defer HTTP parsing until framing and header limits have passed.
            records = ArchiveIterator(stream, verify_http=True, arc2warc=False, no_record_parse=True)
            record = next(records)
            headers = record.rec_headers
            block, target, date = arc_frame if arc_frame is not None else _warc_frame(decoded, headers, block_start)
            if record.rec_type != "response":
                raise UnsupportedRecord(f"unsupported source record type: {record.rec_type}")
            payload_start = _validate_http_headers(block)
            record.http_headers = records.loader.load_http_headers(record.rec_type, target, record.raw_stream, record.length)
            if record.http_headers is None:
                raise UnsupportedRecord("unsupported source response without HTTP headers")
            if any(
                name.lower() == "x-commoncrawl-contenttruncated" and value.strip()
                for name, value in record.http_headers.headers
            ):
                raise SourceTruncated(f"source {record.format.upper()} declares truncation")
            body = record.raw_stream.read()
            if body != block[payload_start:]:
                raise CorruptRecord("incomplete HTTP block consumption")
            # Compute BOTH checks independently over exact bytes, never relying on
            # warcio's aggregate digest flag or its lenient EOF handling.
            for name, value in headers.headers:
                if name.lower() == "warc-block-digest":
                    _verify_digest(value, block, "block")
                elif name.lower() == "warc-payload-digest":
                    _verify_digest(value, body, "payload")
            if next(records, None) is not None:
                raise CorruptRecord("range contained multiple archive records")
            identity = capture.identity
            status = record.http_headers.get_statuscode()
            if not re.fullmatch(r"[1-5][0-9]{2}", status):
                raise CorruptRecord("invalid original HTTP status")
            source_url = _recovery_url(target) if repairs else normalize_original_url(target)
            indexed_url = _recovery_url(identity.original_url) if repairs else identity.original_url
            if (source_url != indexed_url
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
                source_repairs=repairs,
            )
    except (CancelledError, CorruptRecord, UnsupportedRecord, SourceTruncated, IdentityMismatch):
        raise
    except Exception as error:
        raise CorruptRecord(f"invalid compressed Common Crawl record: {error}") from error


def _warc_frame(decoded: bytes, headers, block_start: int) -> tuple[bytes, str, str]:
    """Validate WARC framing using source byte offsets, not parser character counts."""
    for name in ("Content-Length", "WARC-Type", "WARC-Date", "WARC-Target-URI"):
        if sum(key.lower() == name.lower() for key, _ in headers.headers) != 1:
            raise CorruptRecord(f"missing or duplicate {name}")
    declared = headers.get_header("Content-Length")
    if not declared.isascii() or not declared.isdigit():
        raise CorruptRecord("invalid WARC Content-Length")
    # warcio's total_len counts decoded characters, not source bytes.
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
    return block, headers.get_header("WARC-Target-URI"), headers.get_header("WARC-Date")


def _arc_header(decoded: bytes, *, recover: bool = False) -> tuple[list[bytes], bytes]:
    end = decoded.find(b"\n", 0, MAX_ARC_HEADER_BYTES + 1)
    if end < 0:
        if len(decoded) > MAX_ARC_HEADER_BYTES:
            raise RecordLimitExceeded("ARC header exceeds size limit")
        raise CorruptRecord("invalid ARC header: missing newline")
    line = decoded[:end]
    fields = line.rsplit(b" ", 4) if recover else line.split(b" ")
    if len(fields) != 5 or any(
        not field or any(byte < 32 or byte == 127 for byte in field) for field in fields
    ):
        raise CorruptRecord("invalid ARC header: expected five nonempty fields")
    target, _, stamp, _, declared = fields
    if target.startswith(b"filedesc://"):
        raise UnsupportedRecord("unsupported ARC file-description record")
    if not target.startswith((b"http://", b"https://")):
        raise UnsupportedRecord("unsupported non-HTTP ARC record")
    if not re.fullmatch(rb"[0-9]{14}", stamp):
        raise CorruptRecord("invalid ARC timestamp")
    try:
        warc_date_to_cdx(cdx_timestamp_to_warc_date(stamp.decode("ascii")))
        if not re.fullmatch(rb"[0-9]+", declared):
            raise ValueError("invalid ARC block length")
        int(declared)
    except ValueError as error:
        raise CorruptRecord(str(error)) from error
    return fields, decoded[end + 1:]


def _arc_frame(decoded: bytes) -> tuple[bytes, str, str]:
    """Validate ARC byte boundaries independently of the permissive parser."""
    fields, remainder = _arc_header(decoded)
    target, _, stamp, _, declared = fields
    length = int(declared)
    block = remainder[:length]
    if len(block) != length or remainder[length:] not in (b"", b"\n"):
        raise CorruptRecord("ARC block length/framing mismatch")
    return block, target.decode("utf-8"), cdx_timestamp_to_warc_date(stamp.decode("ascii"))


def _validate_http_headers(block: bytes) -> int:
    """Bound both formats' HTTP headers and reject lines warcio would silently discard."""
    end = _header_end(block, max_bytes=MAX_HTTP_HEADER_BYTES, kind="HTTP")
    with BytesIO(block[:end]) as stream:
        status = stream.readline()
        if (not re.fullmatch(rb"HTTP/(?:1\.[01]|2(?:\.0)?|3(?:\.0)?) [1-5][0-9]{2}(?: [^\r\n]*)?\r?\n", status)
                or any(byte < 32 and byte != 9 or byte == 127 for byte in status.rstrip(b"\r\n"))):
            raise CorruptRecord("invalid HTTP status line")
        has_header = False
        while line := stream.readline():
            if line in (b"\r\n", b"\n"):
                return end
            value = line.removesuffix(b"\n").removesuffix(b"\r")
            if any(byte < 32 and byte != 9 or byte == 127 for byte in value):
                raise CorruptRecord("invalid HTTP header")
            if value.startswith((b" ", b"\t")) and has_header:
                continue  # Historical folded headers are parsed by warcio.
            name, separator, _ = value.partition(b":")
            if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise CorruptRecord("invalid HTTP header")
            has_header = True
    raise CorruptRecord("missing HTTP header terminator")


def _recovery_url(value: str) -> str:
    """Permit only space/%20 equivalence in paths and queries, never authority repair."""
    if (any(ord(char) < 32 or ord(char) == 127 for char in value)
            or "\\" in value or re.search(r"%(?![0-9A-Fa-f]{2})", value)):
        raise CorruptRecord("invalid ARC recovery URL")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if (parts.scheme not in ("http", "https") or not host or parts.username is not None
                or any(char in parts.netloc for char in " \\%") or " " in parts.fragment):
            raise ValueError("invalid authority or URL")
        if ":" in host:
            ipaddress.IPv6Address(host)
            authority = r"\[[0-9A-Fa-f:.]+\](?::[0-9]+)?"
        else:
            if (len(host.rstrip(".")) > 253 or any(
                not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.removesuffix(".").split(".")
            )):
                raise ValueError("invalid hostname")
            authority = r"[A-Za-z0-9.-]+(?::[0-9]+)?"
        if not re.fullmatch(authority, parts.netloc):
            raise ValueError("invalid authority")
        parts.port  # Force validation of the port without changing it.
    except ValueError as error:
        raise CorruptRecord("invalid ARC recovery URL") from error
    return normalize_original_url(value.replace(" ", "%20"))


def _recover_arc(decoded: bytes, capture: CaptureRef) -> CaptureResult:
    """Try at most two whole-block candidates; never search or alter payload bytes."""
    fields, remainder = _arc_header(decoded, recover=True)
    try:
        target = fields[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise CorruptRecord("invalid ARC recovery URL encoding") from error
    _recovery_url(target)
    if normalize_payload_digest(capture.identity.payload_digest) is None:
        raise MalformedArcRecord("ARC recovery could not verify payload: missing indexed digest")
    url_repairs = ("arc-url-spaces",) if " " in target else ()
    fields[0] = target.replace(" ", "%20").encode("utf-8")
    declared = int(fields[4])
    accepted = []
    lengths = (len(remainder), len(remainder) - 1) if remainder.endswith(b"\n") else (len(remainder),)
    for length in lengths:
        repairs = url_repairs + (("arc-block-length",) if length != declared else ())
        if not repairs:
            continue
        fields[4] = str(length).encode("ascii")
        candidate = b" ".join(fields) + b"\n" + remainder
        try:
            result = _decode_record(candidate, capture, repairs=repairs)
        except CorruptRecord:
            continue
        if result.digest_matched:
            accepted.append(result)
    if len(accepted) != 1:
        reason = "ambiguous candidates" if accepted else "indexed digest or record validation failed"
        raise MalformedArcRecord(f"ARC recovery could not verify payload: {reason}")
    return accepted[0]


def _header_end(data: bytes, *, max_bytes: int, kind: str) -> int:
    """Find the byte boundary with bounded reads before handing headers to warcio."""
    with BytesIO(data) as stream:
        while line := stream.readline(max_bytes - stream.tell() + 1):
            if stream.tell() > max_bytes:
                raise RecordLimitExceeded(f"{kind} headers exceed size limit")
            if line in (b"\r\n", b"\n"):
                return stream.tell()
    raise CorruptRecord(f"missing {kind} header terminator")


def _verify_digest(value: str, data: bytes, kind: str) -> None:
    try:
        digester, supplied = parse_warc_digest(value)
        digester.update(data)
        digest = digester.digest()
    except (ValueError, TypeError) as error:
        raise CorruptRecord(f"unsupported or invalid {kind} digest") from error
    if not hmac.compare_digest(digest, supplied):
        raise CorruptRecord(f"WARC {kind} digest mismatch")
