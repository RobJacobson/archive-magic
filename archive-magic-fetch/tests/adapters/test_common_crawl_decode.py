"""Strict source validation, independently of warcio's permissive reader."""

import base64
import gzip
import hashlib
from dataclasses import replace
from io import BytesIO
from functools import partial

import pytest
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.adapters.interpret_common_crawl_failures import CorruptRecord, IdentityMismatch, MalformedArcRecord, MalformedWarcRecord, SourceTruncated, UnsupportedRecord
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.models import CommonCrawlLocator
from archive_magic_fetch.pipeline.decoding.decode_common_crawl_capture import decode_common_crawl_capture
from archive_magic_fetch.pipeline.write_captures import _build_response_record, _serialize_record
from common_crawl_helpers import record


@pytest.fixture(params=["warc", "arc"])
def source_record(request):
    return partial(record, format=request.param)


@pytest.mark.parametrize("encoding", ["hex", "base32", "base64"])
@pytest.mark.parametrize("matches", [True, False])
def test_source_digest_encodings_preserve_validation(encoding, matches):
    digest = hashlib.sha1(b"hello" if matches else b"wrong").digest()
    token = (digest.hex() if encoding == "hex" else
             base64.b32encode(digest).decode().lower() if encoding == "base32" else
             base64.b64encode(digest).decode())
    capture, data = record(warc_headers=(("WARC-Payload-Digest", "sha1:" + token),))
    if matches:
        assert decode_common_crawl_capture(data, capture).body == b"hello"
    else:
        with pytest.raises(MalformedWarcRecord, match="payload digest mismatch"):
            decode_common_crawl_capture(data, capture)


def test_raw_payload_and_ordered_repeated_headers_survive_output(source_record):
    capture, data = source_record(headers=(("Set-Cookie", "first=1"), ("Content-Encoding", "gzip"),
                                    ("Transfer-Encoding", "chunked"), ("Content-Length", "900"),
                                    ("Set-Cookie", "second=2"), ("Content-Type", "text/html")))
    result = decode_common_crawl_capture(data, capture)
    assert result.body == b"hello" and result.digest_matched
    assert [pair for pair in result.headers if pair[0] == "Set-Cookie"] == [("Set-Cookie", "first=1"), ("Set-Cookie", "second=2")]
    with BytesIO(_serialize_record(_build_response_record(result))) as stream:
        parsed = next(ArchiveIterator(stream, check_digests="raise"))
        assert parsed.content_stream().read() == b"hello"
        assert [pair for pair in parsed.http_headers.headers if pair[0] == "Set-Cookie"] == [("Set-Cookie", "first=1"), ("Set-Cookie", "second=2")]
        assert parsed.http_headers.get_header("Content-Length") == "5"
        assert parsed.http_headers.get_header("Transfer-Encoding") is None


@pytest.mark.parametrize("mutation", [
    lambda b: b[:-8], lambda b: b[:-3],
    lambda b: b[:-8] + bytes([b[-8] ^ 1]) + b[-7:],
    lambda b: b[:-4] + bytes([b[-4] ^ 1]) + b[-3:],
    lambda b: b + b, lambda b: b + b"garbage", lambda b: b"not gzip",
    lambda b: gzip.compress(gzip.decompress(b) + gzip.decompress(b)),
])
def test_gzip_completion_crc_size_single_member_and_single_record(mutation, source_record):
    capture, data = source_record()
    with pytest.raises((CorruptRecord, MalformedArcRecord, MalformedWarcRecord)):
        decode_common_crawl_capture(mutation(data), capture)


@pytest.mark.parametrize("delta", [-1, 1, 4, 100])
@pytest.mark.parametrize("digests", [True, False])
def test_warc_length_must_match_even_when_checks_never_complete(delta, digests):
    fields = () if digests else (("WARC-Payload-Digest", None), ("WARC-Block-Digest", None))
    capture, data = record(length_delta=delta, warc_headers=fields)
    with pytest.raises(MalformedWarcRecord, match="length/framing"):
        decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("name", ["WARC-Payload-Digest", "WARC-Block-Digest"])
def test_each_supplied_digest_is_verified(name):
    capture, data = record(warc_headers=((name, payload_digest(b"wrong")),))
    with pytest.raises(MalformedWarcRecord, match="digest mismatch"):
        decode_common_crawl_capture(data, capture)


def test_extra_crlf_2018_34_is_rejected_without_repair():
    # Reproduce the erratum: an extra CRLF after HTTP headers, while the
    # advertised payload digest still describes the original payload.
    capture, data = record(body=b"\r\nhello", headers=(("Content-Length", "5"),),
                           warc_headers=(("WARC-Payload-Digest", payload_digest(b"hello")),))
    with pytest.raises(MalformedWarcRecord, match="payload digest mismatch"):
        decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("changes", [{"timestamp": "20170616000000"}, {"original_url": "https://example.org/other"}, {"status_token": "404"}])
def test_identity_mismatch(changes, source_record):
    capture, data = source_record()
    with pytest.raises(IdentityMismatch):
        decode_common_crawl_capture(data, replace(capture, identity=replace(capture.identity, **changes)))


def test_index_digest_mismatch_and_missing_digest_do_not_seed_reuse(source_record):
    capture, data = source_record()
    for digest in (payload_digest(b"other"), "-"):
        result = decode_common_crawl_capture(data, replace(capture, identity=replace(capture.identity, payload_digest=digest)))
        assert result.body == b"hello" and not result.digest_matched
    assert decode_common_crawl_capture(data, capture).digest_matched


@pytest.mark.parametrize("status", ["301", "302", "404", "206"])
def test_empty_and_non_200_responses(status, source_record):
    capture, data = source_record(status=status, body=b"", headers=(("Location", "/next"),))
    result = decode_common_crawl_capture(data, capture)
    assert result.status_code == int(status) and result.body == b"" and result.digest_matched


@pytest.mark.parametrize("kind,fields,error", [
    ("revisit", (), UnsupportedRecord), ("resource", (), UnsupportedRecord),
    ("response", (("WARC-Truncated", "length"),), SourceTruncated),
    ("response", (("WARC-Segment-Number", "1"),), UnsupportedRecord),
])
def test_unsupported_records(kind, fields, error):
    capture, data = record(kind=kind, warc_headers=fields)
    with pytest.raises(error):
        decode_common_crawl_capture(data, capture)


def test_arc_without_http_headers_is_rejected():
    capture, _ = record()
    with pytest.raises(MalformedArcRecord):
        decode_common_crawl_capture(gzip.compress(b"http://example.org/ 1.2.3.4 20170615000000 text/html 5\nhello\n"), capture)


@pytest.mark.parametrize("separator", [b"", b"\n"])
@pytest.mark.parametrize("body", [b"hello", b"hello\n", b"", "résumé\r\n正文".encode()])
def test_arc_exact_payload_and_optional_separator(separator, body):
    capture, data = record(format="arc", body=body, headers=(("X-Title", "café"),))
    raw = gzip.decompress(data)[:-1] + separator
    result = decode_common_crawl_capture(gzip.compress(raw), capture)
    assert result.body == body and result.digest_matched
    assert ("X-Title", "café") in result.headers
    assert result.warc_date == "2017-06-15T00:00:00Z"
    assert result.source_uri.endswith(capture.locator.filename)


@pytest.mark.parametrize("delta", [-1, 1, 100])
def test_arc_incorrect_declared_length_is_recovered(delta):
    capture, data = record(format="arc", length_delta=delta)
    # Exclude the optional separator so +1 cannot legitimately include it.
    result = decode_common_crawl_capture(gzip.compress(gzip.decompress(data)[:-1]), capture)
    assert result.body == b"hello" and result.digest_matched
    assert result.source_repairs == ("arc-block-length",)


@pytest.mark.parametrize("suffix", [b"\n\n", b"\r\n", b"garbage"])
def test_arc_rejects_extra_trailing_bytes(suffix):
    capture, data = record(format="arc")
    with pytest.raises(MalformedArcRecord):
        decode_common_crawl_capture(gzip.compress(gzip.decompress(data)[:-1] + suffix), capture)


@pytest.mark.parametrize("field,value", [
    (0, b""), (1, b""), (3, b""), (1, b"1.2.3.4\t"),
    (2, b"20170615"), (2, b"20171315000000"), (2, b"20170230000000"),
    (2, b"20170615250000"), (2, "２０１７０６１５００００００".encode()),
    (4, b"-1"), (4, b"+1"), (4, b"1.5"), (4, b""), (4, "５".encode()),
])
def test_arc_invalid_header_fields(field, value):
    capture, data = record(format="arc")
    line, block = gzip.decompress(data).split(b"\n", 1)
    fields = line.split(b" ")
    fields[field] = value
    with pytest.raises(MalformedArcRecord):
        decode_common_crawl_capture(gzip.compress(b" ".join(fields) + b"\n" + block), capture)


@pytest.mark.parametrize("raw", [b"", b"not an ARC record", b"https://example.org/ 1.2.3.4"])
def test_arc_incomplete_header(raw):
    capture, _ = record(format="arc")
    with pytest.raises(MalformedArcRecord, match="invalid ARC header"):
        decode_common_crawl_capture(gzip.compress(raw), capture)


@pytest.mark.parametrize("url,reason", [
    (b"filedesc://archive.arc", "file-description"),
    (b"dns:example.org", "non-HTTP"), (b"ftp://example.org/", "non-HTTP"),
])
def test_arc_unsupported_record_types(url, reason):
    capture, data = record(format="arc")
    raw = gzip.decompress(data).replace(b"https://example.org/", url, 1)
    with pytest.raises(UnsupportedRecord, match=reason):
        decode_common_crawl_capture(gzip.compress(raw), capture)


@pytest.mark.parametrize("marker", ["TruncatedInDownload", "TruncatedInInflate", "TruncatedInDownload,TruncatedInInflate"])
def test_source_truncation_is_reported(marker, source_record):
    capture, data = source_record(headers=(("X-CoMmOnCrAwL-ContentTruncated", marker),))
    with pytest.raises(SourceTruncated, match="source (ARC|WARC) declares truncation"):
        decode_common_crawl_capture(data, capture)


def test_empty_truncation_marker_is_not_truncation(source_record):
    capture, data = source_record(headers=(("x-commoncrawl-ContentTruncated", ""),))
    assert decode_common_crawl_capture(data, capture).body == b"hello"


def test_arc_index_digest_has_no_wayback_newline_tolerance():
    capture, data = record(format="arc")
    capture = replace(capture, identity=replace(capture.identity, payload_digest=payload_digest(b"hello\n")))
    result = decode_common_crawl_capture(data, capture)
    assert result.body == b"hello" and not result.digest_matched


@pytest.mark.parametrize("status", ["000", "600", "20", "abc"])
def test_invalid_http_status_is_rejected(source_record, status):
    capture, data = source_record()
    raw = gzip.decompress(data).replace(b"HTTP/1.1 200", b"HTTP/1.1 " + status.encode(), 1)
    with pytest.raises((CorruptRecord, MalformedArcRecord, MalformedWarcRecord)):
        decode_common_crawl_capture(gzip.compress(raw), capture)


def test_unknown_index_status_uses_source_status(source_record):
    capture, data = source_record(status="404")
    capture = replace(capture, identity=replace(capture.identity, status_token="-"))
    assert decode_common_crawl_capture(data, capture).status_code == 404


def test_format_is_detected_from_bytes(source_record):
    capture, data = source_record()
    other = "arc" if capture.locator.filename.endswith(".warc.gz") else "warc"
    capture = replace(capture, locator=replace(capture.locator, filename=f"capture.{other}.gz"))
    assert decode_common_crawl_capture(data, capture).body == b"hello"


def test_unsupported_warc_version_is_not_parsed_as_arc():
    capture, data = record()
    raw = gzip.decompress(data).replace(b"WARC/1.0", b"WARC/0.18", 1)
    with pytest.raises(UnsupportedRecord, match="unsupported WARC version"):
        decode_common_crawl_capture(gzip.compress(raw), capture)


@pytest.mark.parametrize("changes", [{"filename": "../file.warc.gz"}, {"filename": "https://evil.test/a"},
    {"filename": "a/%2e%2e/b"}, {"filename": "/a"}, {"filename": "a?query"}, {"filename": "a\\b"},
    {"offset": -1}, {"length": 0}, {"offset": True}, {"crawl_id": "../2017"}])
def test_locator_validation(changes):
    values = dict(crawl_id="CC-MAIN-2017-26", filename="crawl-data/a.warc.gz", offset=0, length=1)
    with pytest.raises(ValueError):
        CommonCrawlLocator(**(values | changes))


@pytest.mark.parametrize("http_utf8,warc_utf8", [(True, False), (False, True), (True, True)])
def test_utf8_header_boundaries_are_measured_in_bytes(http_utf8, warc_utf8):
    body = "résumé\r\n\r\n正文".encode()
    http_headers = (("X-Title", "café"),) if http_utf8 else ()
    warc_headers = (("X-Title", "café"),) if warc_utf8 else ()
    capture, data = record(body=body, headers=http_headers, warc_headers=warc_headers)
    result = decode_common_crawl_capture(data, capture)
    assert result.body == body
    assert result.digest_matched and result.warc_payload_digest == payload_digest(body)
    if http_utf8:
        assert ("X-Title", "café") in result.headers


@pytest.mark.parametrize("delta", [-1, 1])
def test_utf8_headers_do_not_relax_warc_block_length_validation(delta):
    capture, data = record(
        headers=(("X-Title", "café"),), warc_headers=(("X-Title", "café"),),
        length_delta=delta,
    )
    with pytest.raises(MalformedWarcRecord, match="length/framing"):
        decode_common_crawl_capture(data, capture)
