import base64
import hashlib
from io import BytesIO

import pytest
from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders

from archive_magic_fetch import export, paths


def payload_digest(payload):
    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def make_response(url, payload=b"payload", status="200"):
    builder = RecordBuilder(warc_version="1.0")
    headers = StatusAndHeaders(
        f"{status} Test",
        [("Content-Type", "text/plain")],
        protocol="HTTP/1.1",
    )
    return builder.create_warc_record(
        url,
        "response",
        payload=BytesIO(payload),
        length=len(payload),
        http_headers=headers,
    )


class FakeCapture(dict):
    def __init__(
        self,
        url,
        timestamp,
        payload=b"payload",
        digest=None,
        status="200",
        error=None,
    ):
        if digest is None:
            digest = payload_digest(payload).split(":", 1)[1]
        super().__init__(
            url=url,
            timestamp=timestamp,
            digest=digest,
            status=status,
        )
        self.payload = payload
        self.error = error
        self.fetch_count = 0

    def fetch_warc_record(self):
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        return make_response(self["url"], self.payload, self["status"])


def read_records(path):
    with path.open("rb") as stream:
        return list(ArchiveIterator(stream))


def output_path(tmp_path, url):
    return paths.warc_path(url, root=tmp_path / "warcs")


def test_normalize_digest_accepts_raw_and_prefixed_sha1():
    raw = payload_digest(b"payload").split(":", 1)[1]

    assert export.normalize_digest(raw.lower()) == f"sha1:{raw}"
    assert export.normalize_digest(f"SHA1:{raw.lower()}") == f"sha1:{raw}"


@pytest.mark.parametrize(
    "value",
    [None, "", "-", "md5:AAAAAAAA", "sha1:short", "!" * 32],
)
def test_normalize_digest_rejects_unusable_values(value):
    assert export.normalize_digest(value) is None


def test_repeated_same_url_digest_writes_response_then_revisit(tmp_path):
    url = "https://example.com/image.png"
    first = FakeCapture(url, "20170101000000")
    second = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_url(url, [first, second], target)

    assert (first.fetch_count, second.fetch_count) == (1, 0)
    records = read_records(target)
    assert [record.rec_type for record in records] == ["warcinfo", "response", "revisit"]
    response, revisit = records[1:]
    assert response.rec_headers.get_header("WARC-Target-URI") == url
    assert response.rec_headers.get_header("WARC-Date") == "2017-01-01T00:00:00Z"
    assert revisit.rec_headers.get_header("WARC-Target-URI") == url
    assert revisit.rec_headers.get_header("WARC-Date") == "2018-01-01T00:00:00Z"
    assert revisit.rec_headers.get_header("WARC-Payload-Digest") == payload_digest(b"payload")
    assert revisit.rec_headers.get_header("WARC-Refers-To") == response.rec_headers.get_header(
        "WARC-Record-ID"
    )
    assert revisit.rec_headers.get_header("WARC-Refers-To-Date") == response.rec_headers.get_header(
        "WARC-Date"
    )


def test_same_digest_at_different_urls_fetches_independently(tmp_path):
    urls = ["https://example.com/a", "https://example.com/b"]
    captures = {
        url: [FakeCapture(url, "20170101000000", payload=b"same")] for url in urls
    }
    output_paths = paths.preflight_paths(captures, root=tmp_path / "warcs")

    export.export_all(captures, output_paths)

    assert [captures[url][0].fetch_count for url in urls] == [1, 1]
    assert all(read_records(output_paths[url])[1].rec_type == "response" for url in urls)


def test_new_digest_uses_full_retrieval_then_later_revisit(tmp_path):
    url = "https://example.com/resource"
    captures = [
        FakeCapture(url, "20170101000000", payload=b"A"),
        FakeCapture(url, "20180101000000", payload=b"B"),
        FakeCapture(url, "20190101000000", payload=b"B"),
    ]
    target = output_path(tmp_path, url)

    export.export_url(url, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 1, 0]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
        "revisit",
    ]


def test_redirects_are_always_full_responses_and_do_not_seed_map(tmp_path):
    url = "https://example.com/redirect"
    captures = [
        FakeCapture(url, "20170101000000", payload=b"", status="301"),
        FakeCapture(url, "20180101000000", payload=b"", status="302"),
    ]
    target = output_path(tmp_path, url)

    export.export_url(url, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 1]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_missing_digest_writes_full_response_and_seeds_calculated_digest(tmp_path):
    url = "https://example.com/missing"
    first = FakeCapture(url, "20170101000000", digest="-")
    second = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_url(url, [first, second], target)

    assert (first.fetch_count, second.fetch_count) == (1, 0)
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_missing_digest_still_writes_full_response_when_actual_was_seen(tmp_path):
    url = "https://example.com/missing-later"
    first = FakeCapture(url, "20170101000000")
    second = FakeCapture(url, "20180101000000", digest="-")
    target = output_path(tmp_path, url)

    export.export_url(url, [first, second], target)

    assert (first.fetch_count, second.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_retrieval_failure_warns_and_later_capture_succeeds(tmp_path, capsys):
    url = "https://example.com/failure"
    failed = FakeCapture(url, "20170101000000", error=RuntimeError("unavailable"))
    successful = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_url(url, [failed, successful], target)

    assert (failed.fetch_count, successful.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == ["warcinfo", "response"]
    assert (
        "WARNING skipped 20170101000000 https://example.com/failure: capture unavailable"
        in capsys.readouterr().err
    )


def test_digest_mismatch_warns_does_not_seed_and_processing_continues(tmp_path, capsys):
    url = "https://example.com/mismatch"
    expected = payload_digest(b"expected").split(":", 1)[1]
    mismatch = FakeCapture(
        url,
        "20170101000000",
        payload=b"different",
        digest=expected,
    )
    successful = FakeCapture(url, "20180101000000", payload=b"expected", digest=expected)
    target = output_path(tmp_path, url)

    export.export_url(url, [mismatch, successful], target)

    assert (mismatch.fetch_count, successful.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == ["warcinfo", "response"]
    assert "payload digest mismatch" in capsys.readouterr().err


def test_all_skipped_url_creates_no_file(tmp_path):
    url = "https://example.com/skipped"
    capture = FakeCapture(url, "20170101000000", error=RuntimeError("unavailable"))
    target = output_path(tmp_path, url)

    export.export_url(url, [capture], target)

    assert not target.exists()
    assert not (tmp_path / "warcs").exists()


def test_local_open_failure_is_fatal_not_a_capture_warning(tmp_path, monkeypatch, capsys):
    url = "https://example.com/local-failure"
    capture = FakeCapture(url, "20170101000000")

    def fail_open(path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(export, "open_new_warc", fail_open)

    with pytest.raises(OSError, match="disk unavailable"):
        export.export_url(url, [capture], output_path(tmp_path, url))

    assert "WARNING" not in capsys.readouterr().err

