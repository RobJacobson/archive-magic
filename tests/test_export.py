import base64
import hashlib
from io import BytesIO

import pytest
from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders

from archive_magic_fetch import discovery, export, paths
from archive_magic_fetch.retrieval import PlaybackSubstitution, RetrievedResponse


URLKEY = "com,example)/resource"


def payload_digest(payload):
    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def make_response(url, payload=b"payload", status="200", location=None):
    builder = RecordBuilder(warc_version="1.0")
    response_headers = [("Content-Type", "text/plain")]
    if location is not None:
        response_headers.append(("Location", location))
    headers = StatusAndHeaders(
        f"{status} Test",
        response_headers,
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
        location=None,
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
        self.location = location
        self.error = error
        self.fetch_count = 0

    def fetch_warc_record(self):
        self.fetch_count += 1
        if self.error is not None:
            raise self.error
        return make_response(
            self["url"],
            self.payload,
            self["status"],
            self.location,
        )


def read_records(path):
    with path.open("rb") as stream:
        return list(ArchiveIterator(stream))


def output_path(tmp_path, url, urlkey=URLKEY):
    return paths.urlkey_warc_path(urlkey, root=tmp_path / "warcs")


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


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        (
            "http://www.example.com:80/path?x=1",
            "https://example.com/path?x=1",
            True,
        ),
        (
            "https://www.example.com/path",
            "https://example.com/other",
            False,
        ),
        (
            "https://www.example.com/path",
            "https://other.example/path",
            False,
        ),
        (
            "http://example.com:443/path",
            "https://example.com:443/path",
            False,
        ),
    ],
)
def test_canonical_alias_redirect_classification(source, target, expected):
    assert export._is_canonical_alias_redirect(source, target) is expected


def test_repeated_same_url_digest_writes_response_then_revisit(tmp_path):
    url = "https://example.com/image.png"
    first = FakeCapture(url, "20170101000000")
    second = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [first, second], target)

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


def test_same_digest_at_url_variants_fetches_once_and_uses_cross_target_revisit(
    tmp_path,
):
    urls = ["https://example.com/a", "https://example.com/b"]
    captures = [
        FakeCapture(url, f"2017010{index}000000", payload=b"same")
        for index, url in enumerate(urls, start=1)
    ]
    groups = {URLKEY: captures}
    output_paths = paths.preflight_paths(groups, root=tmp_path / "warcs")

    export.export_all(groups, output_paths)

    assert [capture.fetch_count for capture in captures] == [1, 0]
    records = read_records(output_paths[URLKEY])
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    assert [
        record.rec_headers.get_header("WARC-Target-URI")
        for record in records[1:]
    ] == urls
    response, revisit = records[1:]
    assert revisit.rec_headers.get_header(
        "WARC-Refers-To"
    ) == response.rec_headers.get_header("WARC-Record-ID")
    assert revisit.rec_headers.get_header(
        "WARC-Refers-To-Target-URI"
    ) == urls[0]
    assert revisit.rec_headers.get_header(
        "WARC-Refers-To-Date"
    ) == response.rec_headers.get_header("WARC-Date")


def test_new_digest_uses_full_retrieval_then_later_revisit(tmp_path):
    url = "https://example.com/resource"
    captures = [
        FakeCapture(url, "20170101000000", payload=b"A"),
        FakeCapture(url, "20180101000000", payload=b"B"),
        FakeCapture(url, "20190101000000", payload=b"B"),
    ]
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 1, 0]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
        "revisit",
    ]


def test_repeated_redirect_digest_and_status_uses_one_download(tmp_path):
    url = "https://example.com/redirect"
    captures = [
        FakeCapture(url, "20170101000000", payload=b"", status="302"),
        FakeCapture(url, "20180101000000", payload=b"", status="302"),
    ]
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 0]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_redirect_digest_and_status_reuses_across_url_variants(tmp_path):
    urls = [
        "http://www.example.com/index.html",
        "http://example.com:80/index.html",
    ]
    captures = [
        FakeCapture(url, f"2017010{index}000000", payload=b"", status="302")
        for index, url in enumerate(urls, start=1)
    ]
    target = output_path(tmp_path, urls[0])

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 0]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    assert records[2].rec_headers.get_header(
        "WARC-Refers-To-Target-URI"
    ) == urls[0]


def test_verified_canonical_alias_redirects_are_omitted_and_summarized(
    tmp_path, capsys
):
    captures = [
        FakeCapture(
            "http://www.example.com:80/index.html",
            "20170101000000",
            payload=b"redirect body",
            status="301",
            location="https://example.com/index.html",
        ),
        FakeCapture(
            "https://www.example.com/index.html",
            "20180101000000",
            payload=b"redirect body",
            status="301",
            location="https://example.com/index.html",
        ),
    ]
    target = output_path(tmp_path, captures[0]["url"])

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 0]
    assert not target.exists()
    output = capsys.readouterr()
    assert output.err == ""
    assert "Omitted 2 canonical URL redirects" in output.out


def test_meaningful_redirect_is_preserved(tmp_path):
    url = "https://example.com/old"
    capture = FakeCapture(
        url,
        "20170101000000",
        payload=b"redirect body",
        status="301",
        location="https://example.com/new",
    )
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [capture], target)

    records = read_records(target)
    assert [record.rec_type for record in records] == ["warcinfo", "response"]
    assert (
        records[1].http_headers.get_header("Location")
        == "https://example.com/new"
    )


def test_wayback_alias_substitution_is_omitted_once_per_source_signature(
    tmp_path, monkeypatch, capsys
):
    captures = [
        FakeCapture(
            "http://www.example.com/index.html",
            "20170101000000",
            payload=b"redirect body",
            status="301",
        ),
        FakeCapture(
            "https://www.example.com/index.html",
            "20180101000000",
            payload=b"redirect body",
            status="301",
        ),
    ]
    calls = []

    def substitute(capture, expected):
        calls.append(capture["url"])
        raise PlaybackSubstitution(
            "Wayback substituted HTTP 302",
            "https://example.com/index.html",
        )

    monkeypatch.setattr(export, "retrieve_response", substitute)
    target = output_path(tmp_path, captures[0]["url"])

    export.export_group(URLKEY, captures, target)

    assert calls == [captures[0]["url"]]
    assert not target.exists()
    output = capsys.readouterr()
    assert output.err == ""
    assert "Omitted 2 canonical URL redirects" in output.out


def test_wayback_meaningful_substitution_warns_and_skips(
    tmp_path, monkeypatch, capsys
):
    capture = FakeCapture(
        "https://example.com/old",
        "20170101000000",
        payload=b"redirect body",
        status="301",
    )

    def substitute(capture, expected):
        raise PlaybackSubstitution(
            "Wayback substituted HTTP 302 (https://other.example/new)",
            "https://other.example/new",
        )

    monkeypatch.setattr(export, "retrieve_response", substitute)
    target = output_path(tmp_path, capture["url"])

    export.export_group(URLKEY, [capture], target)

    assert not target.exists()
    assert "Wayback substituted HTTP 302" in capsys.readouterr().err


def test_redirect_status_change_requires_a_new_download(tmp_path):
    url = "https://example.com/redirect"
    captures = [
        FakeCapture(url, "20170101000000", payload=b"", status="301"),
        FakeCapture(url, "20180101000000", payload=b"", status="302"),
    ]
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 1]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_source_revisit_uses_verified_digest_without_download(tmp_path):
    url = "https://example.com/redirect"
    first = FakeCapture(url, "20170101000000", payload=b"", status="302")
    revisit = FakeCapture(url, "20180101000000", payload=b"", status="-")
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [first, revisit], target)

    assert (first.fetch_count, revisit.fetch_count) == (1, 0)
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_distinct_source_digests_normalize_to_one_payload(
    tmp_path, monkeypatch
):
    url = "https://example.com/content"
    captures = [
        FakeCapture(url, "20170101000000", digest=payload_digest(b"source-a")),
        FakeCapture(url, "20180101000000", digest=payload_digest(b"source-b")),
        FakeCapture(url, "20190101000000", digest=payload_digest(b"source-b")),
    ]
    for capture in captures:
        capture.payload = b"normalized"

    def retrieve(capture, expected):
        return RetrievedResponse(
            record=capture.fetch_warc_record(),
            source_verified=True,
        )

    monkeypatch.setattr(export, "retrieve_response", retrieve)
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, captures, target)

    assert [capture.fetch_count for capture in captures] == [1, 1, 0]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
        "revisit",
    ]
    assert {
        record.rec_headers.get_header("WARC-Payload-Digest")
        for record in records[1:]
    } == {payload_digest(b"normalized")}


def test_new_source_digest_is_verified_then_reuses_normalized_content(tmp_path):
    url = "https://example.com/missing"
    first = FakeCapture(url, "20170101000000", digest="-")
    second = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [first, second], target)

    assert (first.fetch_count, second.fetch_count) == (1, 1)
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

    export.export_group(URLKEY, [first, second], target)

    assert (first.fetch_count, second.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_retrieval_failure_warns_and_later_capture_succeeds(tmp_path, capsys):
    url = "https://example.com/failure"
    failed = FakeCapture(url, "20170101000000", error=RuntimeError("unavailable"))
    successful = FakeCapture(url, "20180101000000")
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [failed, successful], target)

    assert (failed.fetch_count, successful.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == ["warcinfo", "response"]
    assert (
        "WARNING skipped 20170101000000 https://example.com/failure: "
        "capture unavailable (RuntimeError: unavailable)"
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

    export.export_group(URLKEY, [mismatch, successful], target)

    assert (mismatch.fetch_count, successful.fetch_count) == (1, 1)
    assert [record.rec_type for record in read_records(target)] == ["warcinfo", "response"]
    assert "payload digest mismatch" in capsys.readouterr().err


def test_all_skipped_url_creates_no_file(tmp_path):
    url = "https://example.com/skipped"
    capture = FakeCapture(url, "20170101000000", error=RuntimeError("unavailable"))
    target = output_path(tmp_path, url)

    export.export_group(URLKEY, [capture], target)

    assert not target.exists()
    assert not (tmp_path / "warcs").exists()


def test_local_open_failure_is_fatal_not_a_capture_warning(tmp_path, monkeypatch, capsys):
    url = "https://example.com/local-failure"
    capture = FakeCapture(url, "20170101000000")

    def fail_open(path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(export, "open_new_warc", fail_open)

    with pytest.raises(OSError, match="disk unavailable"):
        export.export_group(URLKEY, [capture], output_path(tmp_path, url))

    assert "WARNING" not in capsys.readouterr().err


def test_nonresource_url_syntax_is_removed_before_fetch_and_warc_identity(
    tmp_path,
):
    capture = FakeCapture(
        "http://www.example.com:80/index.html?#content-primary",
        "20060114082621",
    )
    capture["urlkey"] = "com,example)/index.html"
    groups = discovery.group_captures([capture])
    target = paths.preflight_paths(groups, root=tmp_path / "warcs")[capture["urlkey"]]

    export.export_all(groups, {capture["urlkey"]: target})

    assert capture.fetch_count == 1
    assert capture["url"] == "http://www.example.com:80/index.html"
    response = read_records(target)[1]
    assert response.rec_headers.get_header("WARC-Target-URI") == capture["url"]
