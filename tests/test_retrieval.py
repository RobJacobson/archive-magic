import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from wayback import Mode
from wayback.exceptions import (
    MementoPlaybackError,
    RateLimitError,
    UnexpectedResponseFormat,
)

from archive_magic_fetch import retrieval


def payload_digest(payload):
    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


class FakeMemento:
    def __init__(
        self,
        *,
        url="https://played.example/resource",
        timestamp=None,
        status_code=200,
        memento_url=(
            "https://web.archive.org/web/20200102030405id_/"
            "https://played.example/resource"
        ),
        headers=None,
        content=b"semantic payload",
    ):
        self.url = url
        self.timestamp = timestamp or datetime(
            2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc
        )
        self.status_code = status_code
        self.memento_url = memento_url
        self.headers = headers or {"Content-Type": "text/plain"}
        self.content = content
        self.entered = False
        self.closed = False
        self.exit_error = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, error_type, error, traceback):
        self.closed = True
        self.exit_error = error


class FakeClient:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def get_memento(self, capture, **kwargs):
        self.calls.append((capture, kwargs))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


def test_retrieve_uses_exact_original_memento_and_maps_all_fields():
    timestamp = datetime(
        2020,
        1,
        2,
        1,
        4,
        5,
        999999,
        tzinfo=timezone(timedelta(hours=-2)),
    )
    memento = FakeMemento(
        timestamp=timestamp,
        headers={
            "Content-Type": "text/html",
            "Cache-Control": "max-age=60",
        },
        content=b"<html>semantic</html>",
    )
    client = FakeClient([memento])
    capture = object()

    response = retrieval.retrieve_response(client, capture)

    assert client.calls == [
        (
            capture,
            {
                "mode": Mode.original,
                "exact": True,
                "follow_redirects": False,
            },
        )
    ]
    assert memento.entered is True
    assert memento.closed is True
    assert response.rec_headers.get_header("WARC-Target-URI") == memento.url
    assert (
        response.rec_headers.get_header("WARC-Date")
        == "2020-01-02T03:04:05Z"
    )
    assert (
        response.rec_headers.get_header("WARC-Source-URI")
        == memento.memento_url
    )
    assert response.http_headers.statusline == "200 OK"
    assert response.http_headers.get_header("Content-Type") == "text/html"
    assert response.http_headers.get_header("Cache-Control") == "max-age=60"
    assert response.content_stream().read() == b"<html>semantic</html>"
    assert response.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == payload_digest(b"<html>semantic</html>")


def test_retrieve_removes_representation_headers_and_sets_semantic_length():
    payload = b"decoded"
    memento = FakeMemento(
        headers={
            "content-encoding": "gzip",
            "Transfer-Encoding": "chunked",
            "CONTENT-LENGTH": "999",
            "ETag": '"stored"',
            "Content-MD5": "old",
            "Content-Digest": "old",
            "Digest": "old",
            "Repr-Digest": "old",
            "Content-Range": "bytes 0-2/9",
            "Content-Type": "text/plain",
        },
        content=payload,
    )

    response = retrieval.retrieve_response(FakeClient([memento]), object())

    for name in (
        "Content-Encoding",
        "Transfer-Encoding",
        "ETag",
        "Content-MD5",
        "Content-Digest",
        "Digest",
        "Repr-Digest",
        "Content-Range",
    ):
        assert response.http_headers.get_header(name) is None
    assert response.http_headers.get_header("Content-Length") == str(
        len(payload)
    )
    assert response.http_headers.get_header("Content-Type") == "text/plain"


def test_retrieve_preserves_genuine_historical_redirect():
    memento = FakeMemento(
        url="http://www.example.com/",
        status_code=301,
        headers={"Location": "https://example.com/"},
        content=b"redirect body",
    )

    response = retrieval.retrieve_response(FakeClient([memento]), object())

    assert response.http_headers.statusline == "301 Moved Permanently"
    assert (
        response.http_headers.get_header("Location")
        == "https://example.com/"
    )
    assert response.content_stream().read() == b"redirect body"


def test_retrieve_unknown_status_has_no_invented_reason():
    response = retrieval.retrieve_response(
        FakeClient([FakeMemento(status_code=599)]),
        object(),
    )

    assert response.http_headers.statusline == "599"


def test_memento_closes_when_warc_construction_fails(monkeypatch):
    memento = FakeMemento()

    class FailingBuilder:
        def __init__(self, **kwargs):
            pass

        def create_warc_record(self, *args, **kwargs):
            raise RuntimeError("cannot build WARC")

    monkeypatch.setattr(retrieval, "RecordBuilder", FailingBuilder)

    with pytest.raises(RuntimeError, match="cannot build WARC"):
        retrieval.retrieve_response(FakeClient([memento]), object())
    assert memento.closed is True
    assert isinstance(memento.exit_error, RuntimeError)


def test_first_rate_limit_sleeps_and_retries_same_capture(monkeypatch):
    sleeps = []
    capture = object()
    memento = FakeMemento()
    client = FakeClient([RateLimitError(None, 11), memento])
    monkeypatch.setattr(retrieval.time, "sleep", sleeps.append)

    retrieval.retrieve_response(client, capture)

    assert sleeps == [11]
    assert [call[0] for call in client.calls] == [capture, capture]


def test_missing_retry_after_sleeps_for_sixty_seconds(monkeypatch):
    sleeps = []
    client = FakeClient([RateLimitError(None, None), FakeMemento()])
    monkeypatch.setattr(retrieval.time, "sleep", sleeps.append)

    retrieval.retrieve_response(client, object())

    assert sleeps == [60]


def test_second_rate_limit_is_fatal(monkeypatch):
    sleeps = []
    client = FakeClient(
        [RateLimitError(None, 2), RateLimitError(None, 3)]
    )
    monkeypatch.setattr(retrieval.time, "sleep", sleeps.append)

    with pytest.raises(RateLimitError):
        retrieval.retrieve_response(client, object())
    assert sleeps == [2]


@pytest.mark.parametrize(
    "error",
    [
        MementoPlaybackError("unavailable"),
        UnexpectedResponseFormat("malformed"),
    ],
)
def test_retrieve_does_not_swallow_wayback_errors(error):
    with pytest.raises(type(error), match=str(error)):
        retrieval.retrieve_response(FakeClient([error]), object())
