"""Strict source validation, independently of warcio's permissive reader."""

import base64
import gzip
import hashlib
from dataclasses import replace
from io import BytesIO

import pytest
from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.adapters.interpret_common_crawl_failures import CorruptRecord, IdentityMismatch, SourceTruncated, UnsupportedRecord
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.models import CommonCrawlLocator
from archive_magic_fetch.pipeline.decoding.decode_warc_capture import decode_warc_capture
from archive_magic_fetch.pipeline.write_captures import _build_response_record, _serialize_record
from common_crawl_helpers import record


@pytest.mark.parametrize("encoding", ["hex", "base32", "base64"])
@pytest.mark.parametrize("matches", [True, False])
def test_source_digest_encodings_preserve_validation(encoding, matches):
    digest = hashlib.sha1(b"hello" if matches else b"wrong").digest()
    token = (digest.hex() if encoding == "hex" else
             base64.b32encode(digest).decode().lower() if encoding == "base32" else
             base64.b64encode(digest).decode())
    capture, data = record(warc_headers=(("WARC-Payload-Digest", "sha1:" + token),))
    if matches:
        assert decode_warc_capture(data, capture).body == b"hello"
    else:
        with pytest.raises(CorruptRecord, match="payload digest mismatch"):
            decode_warc_capture(data, capture)


def test_raw_payload_and_ordered_repeated_headers_survive_output():
    capture, data = record(headers=(("Set-Cookie", "first=1"), ("Content-Encoding", "gzip"),
                                    ("Transfer-Encoding", "chunked"), ("Content-Length", "900"),
                                    ("Set-Cookie", "second=2"), ("Content-Type", "text/html")))
    result = decode_warc_capture(data, capture)
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
def test_gzip_completion_crc_size_single_member_and_single_record(mutation):
    capture, data = record()
    with pytest.raises(CorruptRecord):
        decode_warc_capture(mutation(data), capture)


@pytest.mark.parametrize("delta", [-1, 1, 4, 100])
@pytest.mark.parametrize("digests", [True, False])
def test_warc_length_must_match_even_when_checks_never_complete(delta, digests):
    fields = () if digests else (("WARC-Payload-Digest", None), ("WARC-Block-Digest", None))
    capture, data = record(length_delta=delta, warc_headers=fields)
    with pytest.raises(CorruptRecord, match="length/framing"):
        decode_warc_capture(data, capture)


@pytest.mark.parametrize("name", ["WARC-Payload-Digest", "WARC-Block-Digest"])
def test_each_supplied_digest_is_verified(name):
    capture, data = record(warc_headers=((name, payload_digest(b"wrong")),))
    with pytest.raises(CorruptRecord, match="digest mismatch"):
        decode_warc_capture(data, capture)


def test_extra_crlf_2018_34_is_rejected_without_repair():
    # Reproduce the erratum: an extra CRLF after HTTP headers, while the
    # advertised payload digest still describes the original payload.
    capture, data = record(body=b"\r\nhello", headers=(("Content-Length", "5"),),
                           warc_headers=(("WARC-Payload-Digest", payload_digest(b"hello")),))
    with pytest.raises(CorruptRecord, match="payload digest mismatch"):
        decode_warc_capture(data, capture)


@pytest.mark.parametrize("changes", [{"timestamp": "20170616000000"}, {"original_url": "https://example.org/other"}, {"status_token": "404"}])
def test_identity_mismatch(changes):
    capture, data = record()
    with pytest.raises(IdentityMismatch):
        decode_warc_capture(data, replace(capture, identity=replace(capture.identity, **changes)))


def test_index_digest_mismatch_and_missing_digest_do_not_seed_reuse():
    capture, data = record()
    for digest in (payload_digest(b"other"), "-"):
        result = decode_warc_capture(data, replace(capture, identity=replace(capture.identity, payload_digest=digest)))
        assert result.body == b"hello" and not result.digest_matched
    assert decode_warc_capture(data, capture).digest_matched


@pytest.mark.parametrize("status", ["301", "302", "404", "206"])
def test_empty_and_non_200_responses(status):
    capture, data = record(status=status, body=b"", headers=(("Location", "/next"),))
    result = decode_warc_capture(data, capture)
    assert result.status_code == int(status) and result.body == b"" and result.digest_matched


@pytest.mark.parametrize("kind,fields,error", [
    ("revisit", (), UnsupportedRecord), ("resource", (), UnsupportedRecord),
    ("response", (("WARC-Truncated", "length"),), SourceTruncated),
    ("response", (("WARC-Segment-Number", "1"),), UnsupportedRecord),
])
def test_unsupported_records(kind, fields, error):
    capture, data = record(kind=kind, warc_headers=fields)
    with pytest.raises(error):
        decode_warc_capture(data, capture)


def test_arc_does_not_get_implicitly_converted():
    capture, _ = record()
    with pytest.raises(UnsupportedRecord):
        decode_warc_capture(gzip.compress(b"http://example.org/ 1.2.3.4 20170615000000 text/html 5\nhello\n"), capture)


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
    result = decode_warc_capture(data, capture)
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
    with pytest.raises(CorruptRecord, match="length/framing"):
        decode_warc_capture(data, capture)
