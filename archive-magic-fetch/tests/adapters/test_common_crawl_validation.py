"""Shared header safeguards and permanent WARC failure classification."""

import gzip
from concurrent.futures import CancelledError
from unittest.mock import Mock

import pytest
from warcio.recordloader import ArcWarcRecordLoader

from archive_magic_fetch.adapters.interpret_common_crawl_failures import (
    CorruptRecord, MalformedArcRecord, MalformedWarcRecord, RecordLimitExceeded,
    SourceTruncated, failure_advice,
)
from archive_magic_fetch.pipeline.decoding import decode_common_crawl_capture as decoder
from common_crawl_helpers import record


@pytest.mark.parametrize("format,error", [("arc", MalformedArcRecord), ("warc", MalformedWarcRecord)])
@pytest.mark.parametrize("name,value", [
    ("Bad Name", "value"), ("", "value"), ("Missing colon\r\nX-Test", "value"),
    (" continuation\r\nX-Test", "value"), ("X-Test", "nul\x00value"), ("X-Test", "bare\rvalue"),
])
def test_malformed_http_headers_are_rejected_even_with_valid_source_digests(format, error, name, value):
    capture, data = record(format=format, headers=((name, value),))
    reason = "invalid HTTP header" if format == "warc" else "ARC recovery could not verify payload"
    with pytest.raises(error, match=reason):
        decoder.decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("format", ["arc", "warc"])
def test_folded_repeated_and_unicode_http_headers_remain_supported(format):
    capture, data = record(format=format, body="résumé\n".encode(), headers=(
        ("X-Title", "café\r\n continued"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
    ))
    result = decoder.decode_common_crawl_capture(data, capture)
    assert result.body == "résumé\n".encode() and result.digest_matched
    assert ("X-Title", "café continued") in result.headers
    assert [pair for pair in result.headers if pair[0] == "Set-Cookie"] == [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]


@pytest.mark.parametrize("format", ["arc", "warc"])
@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_http_header_limit_counts_original_bytes_including_terminator(monkeypatch, format, headroom):
    capture, data = record(format=format, headers=(("X-Title", "café"),), body=b"x" * 10000)
    raw = gzip.decompress(data)
    block = raw.split(b"\n", 1)[1] if format == "arc" else raw.split(b"\r\n\r\n", 1)[1]
    size = block.index(b"\r\n\r\n") + 4
    monkeypatch.setattr(decoder, "MAX_HTTP_HEADER_BYTES", size + headroom)
    if headroom < 0:
        parse = Mock(side_effect=AssertionError("HTTP parser must not see oversized headers"))
        monkeypatch.setattr(ArcWarcRecordLoader, "load_http_headers", parse)
        with pytest.raises(RecordLimitExceeded, match="HTTP headers exceed size limit"):
            decoder.decode_common_crawl_capture(data, capture)
        parse.assert_not_called()
    else:
        assert decoder.decode_common_crawl_capture(data, capture).body == b"x" * 10000


@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_warc_header_limit_checked_before_archive_parser(monkeypatch, headroom):
    capture, data = record(warc_headers=(("X-Title", "café"),))
    size = gzip.decompress(data).index(b"\r\n\r\n") + 4
    monkeypatch.setattr(decoder, "MAX_WARC_HEADER_BYTES", size + headroom)
    if headroom < 0:
        parse = Mock(side_effect=AssertionError("archive parser must not see oversized headers"))
        monkeypatch.setattr(decoder, "ArchiveIterator", parse)
        with pytest.raises(RecordLimitExceeded, match="WARC headers exceed size limit"):
            decoder.decode_common_crawl_capture(data, capture)
        parse.assert_not_called()
    else:
        assert decoder.decode_common_crawl_capture(data, capture).body == b"hello"


@pytest.mark.parametrize("kind", ["WARC", "HTTP"])
def test_missing_header_terminator_cannot_bypass_limit(monkeypatch, kind):
    capture, data = record()
    raw = gzip.decompress(data)
    if kind == "WARC":
        raw = b"WARC/1.0\r\nX-Huge: " + b"x" * 1000
    else:
        capture, data = record(headers=(("X-Huge", "x" * 1000),))
        raw = gzip.decompress(data).replace(b"\r\n\r\nhello", b"\r\nX-Last:hello", 1)
        # Keep framing valid so validation reaches the HTTP header section.
        head, block = raw.split(b"\r\n\r\n", 1)
        lines = head.split(b"\r\n")
        lines = [b"Content-Length: " + str(len(block) - 4).encode() if line.startswith(b"Content-Length:") else line for line in lines]
        raw = b"\r\n".join(lines) + b"\r\n\r\n" + block
    monkeypatch.setattr(decoder, f"MAX_{kind}_HEADER_BYTES", 64)
    with pytest.raises(RecordLimitExceeded, match=f"{kind} headers exceed size limit"):
        decoder.decode_common_crawl_capture(gzip.compress(raw), capture)


@pytest.mark.parametrize("fields", [
    (("Content-Length", "５"),), (("Content-Length", None),), (("WARC-Date", "invalid"),),
    (("WARC-Payload-Digest", "sha1:invalid"),), (("WARC-Block-Digest", "sha1:invalid"),),
])
def test_intact_gzip_with_bad_warc_metadata_is_nonretryable(fields):
    capture, data = record(warc_headers=fields)
    with pytest.raises(MalformedWarcRecord) as caught:
        decoder.decode_common_crawl_capture(data, capture)
    advice = failure_advice(caught.value, 1)
    assert not advice.retryable and advice.category.value == "unavailable"
    assert advice.display_reason == "malformed WARC"


@pytest.mark.parametrize("format", ["arc", "warc"])
def test_duplicate_truncation_marker_cannot_hide_nonempty_value(format):
    capture, data = record(format=format, headers=(
        ("x-commoncrawl-ContentTruncated", ""), ("X-CommonCrawl-ContentTruncated", "TruncatedInDownload"),
    ))
    with pytest.raises(SourceTruncated):
        decoder.decode_common_crawl_capture(data, capture)


def test_warc_gzip_failure_and_cancellation_are_not_classified_as_malformed(monkeypatch):
    capture, data = record()
    with pytest.raises(CorruptRecord):
        decoder.decode_common_crawl_capture(data[:-4], capture)
    monkeypatch.setattr(decoder, "_decode_record", Mock(side_effect=CancelledError()))
    with pytest.raises(CancelledError):
        decoder.decode_common_crawl_capture(data, capture)
