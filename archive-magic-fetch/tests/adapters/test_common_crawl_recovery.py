"""Verified ARC repair, resource bounds, and failure isolation."""

import gzip
from dataclasses import replace
from unittest.mock import Mock

import pytest

from archive_magic_fetch.adapters.interpret_common_crawl_failures import (
    CorruptRecord, IdentityMismatch, MalformedArcRecord, RecordLimitExceeded,
    SourceTruncated, failure_advice,
)
from archive_magic_fetch.archive.identity import make_identity, payload_digest
from archive_magic_fetch.pipeline.decoding import decode_common_crawl_capture as decoder
from common_crawl_helpers import record


def damaged_arc(*, source_url="https://example.org/", indexed_url=None, length_delta=0, body=b"hello", headers=()):
    capture, data = record(format="arc", length_delta=length_delta, body=body, headers=headers)
    raw = gzip.decompress(data).replace(b"https://example.org/", source_url.encode(), 1)
    capture = replace(capture, identity=make_identity(
        original_url=indexed_url or source_url, timestamp=capture.identity.timestamp,
        status_token="200", payload_digest=payload_digest(body),
    ))
    return capture, gzip.compress(raw)


@pytest.mark.parametrize("indexed_url", ["https://example.org/some path?q=some value", "https://example.org/some%20path?q=some%20value"])
@pytest.mark.parametrize("delta", [0, -5, 100])
@pytest.mark.parametrize("body", [b"hello", b"hello\n", b"hello\n\n", b""])
def test_recovery_preserves_exact_payload(indexed_url, delta, body):
    capture, data = damaged_arc(source_url="https://example.org/some path?q=some value",
                                indexed_url=indexed_url, length_delta=delta, body=body)
    result = decoder.decode_common_crawl_capture(data, capture)
    assert result.identity == capture.identity
    assert result.body == body and result.digest_matched
    assert result.source_repairs == ("arc-url-spaces",) + (("arc-block-length",) if delta else ())


@pytest.mark.parametrize("digest", ["-", "sha1:not-valid", payload_digest(b"different"), payload_digest(b"hello\n")])
def test_recovery_requires_exact_indexed_digest(digest):
    capture, data = damaged_arc(length_delta=100)
    capture = replace(capture, identity=replace(capture.identity, payload_digest=digest))
    # body + separator is itself a candidate, so the trailing-newline digest can
    # verify that candidate; use no separator to prove no invented newline is used.
    data = gzip.compress(gzip.decompress(data)[:-1])
    with pytest.raises(MalformedArcRecord, match="could not verify payload"):
        decoder.decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("changes", [
    {"original_url": "https://other.example/"}, {"timestamp": "20170616000000"}, {"status_token": "404"},
])
def test_recovery_requires_capture_identity(changes):
    capture, data = damaged_arc(length_delta=100)
    capture = replace(capture, identity=replace(capture.identity, **changes))
    with pytest.raises(IdentityMismatch):
        decoder.decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("url", [
    "https://bad host/path", "https://user@example.org/path", "https://example.org:bad/path",
    "https://example.org:99999/path", "https://example.org\\other/path", "https://example.org%20other/path",
    "https:///path", "https://[broken/path", "https://example.org/path\tbad", "https://example.org/path#bad fragment",
    "https://[::1]extra/path", "https://example.org:/path", "https://bad..host/path",
    "https://-bad.host/path", "https://example.org/path%zz", "https://example.org/path\\bad",
])
def test_recovery_never_guesses_or_repairs_authority(url):
    capture, data = damaged_arc(length_delta=100)
    raw = gzip.decompress(data).replace(b"https://example.org/", url.encode(), 1)
    with pytest.raises(MalformedArcRecord):
        decoder.decode_common_crawl_capture(gzip.compress(raw), capture)


def test_recovery_does_not_hide_source_truncation():
    capture, data = damaged_arc(length_delta=100, headers=(("X-CommonCrawl-ContentTruncated", "TruncatedInInflate"),))
    with pytest.raises(SourceTruncated):
        decoder.decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("delta", [0, 100])
@pytest.mark.parametrize("line", [b"Missing colon", b": missing name", b"Bad Name: value",
    b"X-Test: value\x00", b"X-Test: value\rbroken", b" continuation without a header"])
def test_arc_never_discards_malformed_http_headers(delta, line):
    capture, data = damaged_arc(length_delta=delta)
    header, block = gzip.decompress(data).split(b"\n", 1)
    status, rest = block.split(b"\n", 1)
    block = status + b"\n" + line + b"\r\n" + rest
    fields = header.split(b" ")
    fields[4] = str(len(block) - 1 + delta).encode()
    with pytest.raises(MalformedArcRecord):
        decoder.decode_common_crawl_capture(gzip.compress(b" ".join(fields) + b"\n" + block), capture)


def test_recovery_preserves_cancellation(monkeypatch):
    from concurrent.futures import CancelledError

    capture, data = damaged_arc(length_delta=100)
    decode = Mock(side_effect=[CorruptRecord("ARC block length/framing mismatch"), CancelledError()])
    monkeypatch.setattr(decoder, "_decode_record", decode)
    with pytest.raises(CancelledError):
        decoder.decode_common_crawl_capture(data, capture)
    assert decode.call_count == 2


def test_recovery_rejects_ambiguous_candidates(monkeypatch):
    capture, data = damaged_arc(length_delta=100)
    # Force a collision so both possible block boundaries appear to verify.
    monkeypatch.setattr(decoder, "payload_digest", lambda _: capture.identity.payload_digest)
    with pytest.raises(MalformedArcRecord, match="ambiguous"):
        decoder.decode_common_crawl_capture(data, capture)


@pytest.mark.parametrize("suffix", [b"junk", b"\n\n", b"\r\n"])
def test_recovery_does_not_search_for_a_matching_substring(suffix):
    capture, data = damaged_arc(length_delta=100)
    raw = gzip.decompress(data)[:-1] + suffix
    with pytest.raises(MalformedArcRecord):
        decoder.decode_common_crawl_capture(gzip.compress(raw), capture)


def test_recovery_rejects_extra_records_and_tries_at_most_two_candidates(monkeypatch):
    capture, data = damaged_arc(length_delta=100)
    _, other = record(format="arc")
    raw = gzip.decompress(data) + gzip.decompress(other)
    decode = Mock(wraps=decoder._decode_record)
    monkeypatch.setattr(decoder, "_decode_record", decode)
    with pytest.raises(MalformedArcRecord):
        decoder.decode_common_crawl_capture(gzip.compress(raw), capture)
    assert decode.call_count == 3  # strict attempt plus two bounded candidates


@pytest.mark.parametrize("mutation", [lambda b: b[:-4], lambda b: b + b, lambda b: b + b"junk",
    lambda b: b[:-8] + bytes([b[-8] ^ 1]) + b[-7:]])
def test_recovery_never_runs_before_gzip_integrity(monkeypatch, mutation):
    capture, data = damaged_arc(length_delta=100)
    recovery = Mock(side_effect=AssertionError("recovery must not run"))
    monkeypatch.setattr(decoder, "_recover_arc", recovery)
    with pytest.raises(CorruptRecord):
        decoder.decode_common_crawl_capture(mutation(data), capture)
    recovery.assert_not_called()


@pytest.mark.parametrize("format", ["arc", "warc"])
@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_decompression_bound_at_boundary(monkeypatch, format, headroom):
    capture, data = record(format=format, body=b"a" * 200000)
    monkeypatch.setattr(decoder, "MAX_DECOMPRESSED_RECORD_BYTES", len(gzip.decompress(data)) + headroom)
    if headroom < 0:
        with pytest.raises(RecordLimitExceeded):
            decoder.decode_common_crawl_capture(data, capture)
    else:
        assert decoder.decode_common_crawl_capture(data, capture).body == b"a" * 200000


@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_arc_header_bound_at_boundary(monkeypatch, headroom):
    capture, data = record(format="arc")
    line = gzip.decompress(data).split(b"\n", 1)[0]
    monkeypatch.setattr(decoder, "MAX_ARC_HEADER_BYTES", len(line) + headroom)
    if headroom < 0:
        with pytest.raises(RecordLimitExceeded):
            decoder.decode_common_crawl_capture(data, capture)
    else:
        assert decoder.decode_common_crawl_capture(data, capture).body == b"hello"


@pytest.mark.parametrize("error", [MalformedArcRecord("malformed ARC"), RecordLimitExceeded("archive record exceeds size limit")])
def test_permanent_failures_are_not_retryable(error):
    advice = failure_advice(error, 1)
    assert not advice.retryable and advice.category.value == "unavailable"
    assert advice.display_reason == str(error)
