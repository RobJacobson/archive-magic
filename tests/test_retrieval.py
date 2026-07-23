import gzip

from archive_magic_fetch import retrieval


class FakeRaw:
    def __init__(self, payload):
        self.payload = payload

    def read(self, decode_content=False):
        assert decode_content is False
        return self.payload


class FakeResponse:
    def __init__(self, payload, headers, status_code=200, reason="OK"):
        self.raw = FakeRaw(payload)
        self.headers = headers
        self.status_code = status_code
        self.reason = reason
        self.closed = False

    def close(self):
        self.closed = True


class PlaybackCapture(dict):
    wb = "https://web.archive.org/web"


def make_capture(status="200"):
    return PlaybackCapture(
        url="http://www.example.com/index.html",
        timestamp="20200812195739",
        status=status,
    )


def test_archived_gzip_is_source_verified_then_normalized():
    content = b"<html>same content</html>"
    archived = gzip.compress(content, mtime=1597265858)
    response = FakeResponse(
        archived,
        {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
            "x-archive-orig-content-length": str(len(archived)),
            "x-archive-orig-etag": '"content+gzip"',
        },
    )

    result = retrieval.fetch_normalized_ia_response(
        make_capture(),
        retrieval._sha1_digest(archived),
        http_get=lambda url: response,
    )

    assert result.source_verified is True
    assert result.record.content_stream().read() == content
    assert result.record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == retrieval._sha1_digest(content)
    assert result.record.http_headers.get_header("Content-Encoding") is None
    assert result.record.http_headers.get_header("Content-Length") == str(
        len(content)
    )
    assert result.record.http_headers.get_header("ETag") is None
    assert response.closed is True


def test_wayback_transport_gzip_decodes_without_changing_source_identity():
    content = b"<html>same content</html>"
    transported = gzip.compress(content, mtime=0)
    response = FakeResponse(
        transported,
        {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
            "x-archive-orig-content-length": str(len(content)),
            "x-archive-orig-etag": '"content+identity"',
        },
    )

    result = retrieval.fetch_normalized_ia_response(
        make_capture(),
        retrieval._sha1_digest(content),
        http_get=lambda url: response,
    )

    assert result.source_verified is True
    assert result.record.content_stream().read() == content
    assert result.record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == retrieval._sha1_digest(content)
    assert result.record.http_headers.get_header("Content-Encoding") is None
    assert (
        result.record.http_headers.get_header("ETag")
        == '"content+identity"'
    )


def test_archived_encoding_header_can_be_exposed_only_as_original_header():
    content = b"<html>same content</html>"
    archived = gzip.compress(content, mtime=1597265858)
    response = FakeResponse(
        archived,
        {
            "x-archive-orig-content-encoding": "gzip",
            "x-archive-orig-content-length": str(len(archived)),
        },
    )

    result = retrieval.fetch_normalized_ia_response(
        make_capture(),
        retrieval._sha1_digest(archived),
        http_get=lambda url: response,
    )

    assert result.record.content_stream().read() == content
    assert result.record.http_headers.get_header("Content-Encoding") is None


def test_source_digest_mismatch_reports_raw_and_decoded_candidates():
    content = b"<html>different content</html>"
    response = FakeResponse(
        gzip.compress(content, mtime=0),
        {"Content-Encoding": "gzip"},
    )

    try:
        retrieval.fetch_normalized_ia_response(
            make_capture(),
            retrieval._sha1_digest(b"expected"),
            http_get=lambda url: response,
        )
    except retrieval.SourceDigestMismatch as error:
        assert "raw sha1:" in str(error)
        assert "decoded sha1:" in str(error)
    else:
        raise AssertionError("expected SourceDigestMismatch")


def test_redirect_status_is_restored_from_cdx():
    response = FakeResponse(
        b"",
        {"x-archive-orig-location": "http://example.org/"},
        status_code=302,
        reason="Found",
    )

    result = retrieval.fetch_normalized_ia_response(
        make_capture(status="301"),
        retrieval._sha1_digest(b""),
        http_get=lambda url: response,
    )

    assert result.record.http_headers.statusline == "301 Moved Permanently"
    assert (
        result.record.http_headers.get_header("Location")
        == "http://example.org/"
    )


def test_wayback_generated_redirect_is_rejected_before_digest_verification():
    response = FakeResponse(
        b"",
        {
            "X-Archive-Redirect-Reason": "found capture at 20200812195800",
            "Location": (
                "https://web.archive.org/web/20200812195800id_/"
                "https://example.com/index.html"
            ),
        },
        status_code=302,
        reason="Found",
    )

    try:
        retrieval.fetch_normalized_ia_response(
            make_capture(status="301"),
            retrieval._sha1_digest(b"archived redirect body"),
            http_get=lambda url: response,
        )
    except retrieval.PlaybackSubstitution as error:
        assert error.target_url == "https://example.com/index.html"
        assert "Wayback substituted HTTP 302" in str(error)
        assert response.closed is True
    else:
        raise AssertionError("expected PlaybackSubstitution")


def test_wayback_substituted_success_exposes_memento_original_target():
    response = FakeResponse(
        b"<html>canonical page</html>",
        {
            "Link": (
                '<https://example.com/index.html>; rel="original", '
                '<https://web.archive.org/>; rel="timegate"'
            ),
        },
        status_code=200,
        reason="OK",
    )

    try:
        retrieval.fetch_normalized_ia_response(
            make_capture(status="301"),
            retrieval._sha1_digest(b"archived redirect body"),
            http_get=lambda url: response,
        )
    except retrieval.PlaybackSubstitution as error:
        assert error.target_url == "https://example.com/index.html"
        assert "Wayback substituted HTTP 200 capture" in str(error)
        assert response.closed is True
    else:
        raise AssertionError("expected PlaybackSubstitution")


def test_unexpected_playback_404_reports_indexed_status():
    response = FakeResponse(
        b"Wayback error page",
        {},
        status_code=404,
        reason="Not Found",
    )

    try:
        retrieval.fetch_normalized_ia_response(
            make_capture(status="200"),
            retrieval._sha1_digest(b"expected"),
            http_get=lambda url: response,
        )
    except retrieval.CaptureRetrievalError as error:
        assert str(error) == "playback returned HTTP 404 for indexed status 200"
    else:
        raise AssertionError("expected CaptureRetrievalError")


def test_archived_500_matching_cdx_status_is_preserved():
    content = b"Archived origin error response"
    response = FakeResponse(
        content,
        {
            "Content-Type": "text/html",
            "x-archive-orig-server": "EOS",
            "x-archive-orig-content-length": str(len(content)),
        },
        status_code=500,
        reason="Internal Server Error",
    )

    result = retrieval.fetch_normalized_ia_response(
        make_capture(status="500"),
        retrieval._sha1_digest(content),
        http_get=lambda url: response,
    )

    assert result.source_verified is True
    assert result.record.http_headers.statusline == "500 Internal Server Error"
    assert result.record.content_stream().read() == content
    assert result.record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == retrieval._sha1_digest(content)


def test_stream_get_does_not_retry_an_indexed_500(monkeypatch):
    response = FakeResponse(
        b"Archived origin error response",
        {},
        status_code=500,
        reason="Internal Server Error",
    )
    calls = []

    monkeypatch.setattr(
        retrieval,
        "get_retries",
        lambda hostname: (0.0, 0.0),
    )
    monkeypatch.setattr(
        retrieval,
        "update_next_fetch",
        lambda hostname, next_fetch: None,
    )

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr(retrieval.requests, "get", get)

    actual = retrieval._stream_get(
        "https://web.archive.org/example",
        expected_status=500,
    )

    assert actual is response
    assert len(calls) == 1
    assert response.closed is False
