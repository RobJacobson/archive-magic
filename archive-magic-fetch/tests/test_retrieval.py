import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ContentDecodingError,
    ReadTimeout,
)
from urllib3.exceptions import IncompleteRead, ProtocolError
from wayback import Mode
from wayback.exceptions import (
    MementoPlaybackError,
    RateLimitError,
    UnexpectedResponseFormat,
    WaybackRetryError,
)

from archive_magic_fetch import retrieval, retry


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


class BrokenEncodingMemento:
    def __init__(self, error):
        self.url = "https://played.example/resource"
        self.timestamp = datetime(
            2020,
            1,
            2,
            3,
            4,
            5,
            tzinfo=timezone.utc,
        )
        self.status_code = 200
        self.memento_url = (
            "https://web.archive.org/web/20200102030405id_/"
            "https://played.example/resource"
        )
        self.headers = {
            "Content-Type": "text/html",
            "Content-Encoding": "gzip",
        }
        self.error = error
        self.closed = False

    @property
    def content(self):
        raise self.error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


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


def test_memento_closes_before_warc_construction_fails(monkeypatch):
    memento = FakeMemento()

    class FailingBuilder:
        def __init__(self, **kwargs):
            pass

        def create_warc_record(self, *args, **kwargs):
            raise RuntimeError("cannot build WARC")

    monkeypatch.setattr(retrieval, "RecordBuilder", FailingBuilder)

    retrieved = retrieval.retrieve_memento(FakeClient([memento]), object())
    assert memento.closed is True
    assert memento.exit_error is None
    with pytest.raises(RuntimeError, match="cannot build WARC"):
        retrieved.to_warc_record()


def _make_retries_immediate(monkeypatch):
    delays = []
    monkeypatch.setattr(retrieval, "sleep_seconds", delays.append)
    return delays


def test_rate_limit_coordinates_backoff_and_retries_same_capture(
    monkeypatch,
    capsys,
):
    delays = _make_retries_immediate(monkeypatch)
    capture = object()
    memento = FakeMemento()
    client = FakeClient([RateLimitError(None, 11), memento])

    retrieval.retrieve_response(client, capture)

    assert delays == [11]
    assert [call[0] for call in client.calls] == [capture, capture]
    output = capsys.readouterr().out
    assert "retry 1/12 in 11s" in output
    assert str(capture) in output


def test_missing_retry_after_uses_exponential_backoff(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient([RateLimitError(None, None), FakeMemento()])

    retrieval.retrieve_response(client, object())

    assert delays == [2]


def test_repeated_rate_limit_exhausts_bounded_attempts(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            RateLimitError(None, attempt)
            for attempt in range(1, 4)
        ]
    )

    with pytest.raises(retry.RetryExhaustedError) as raised:
        retrieval.retrieve_response(client, object(), retries=2)

    assert delays == [2, 4]
    assert len(client.calls) == 3
    assert raised.value.attempts == 3


def test_zero_retries_makes_one_attempt_without_sleep(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient([_connection_refused_retry_error()])

    with pytest.raises(retry.RetryExhaustedError) as raised:
        retrieval.retrieve_response(client, object(), retries=0)

    assert len(client.calls) == 1
    assert delays == []
    assert raised.value.attempts == 1


def test_retryable_service_status_uses_application_backoff(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    error = retry.RetryableWaybackResponseError(
        status_code=503,
        url="https://web.archive.org/test",
        retry_after=9,
    )
    client = FakeClient([error, FakeMemento()])

    retrieval.retrieve_response(client, object(), retries=1)

    assert len(client.calls) == 2
    assert delays == [9]


def _connection_refused_retry_error():
    return WaybackRetryError(
        3,
        8.0,
        ConnectionError(
            "HTTPSConnectionPool(host='web.archive.org', port=443): "
            "Max retries exceeded with url: /web/20200101000000id_/https://example.com/ "
            "(Caused by NewConnectionError("
            "\"HTTPSConnection(host='web.archive.org', port=443): "
            "Failed to establish a new connection: [Errno 61] Connection refused\"))"
        ),
    )


def _truncated_retry_error(received=130810, remaining=144219):
    return WaybackRetryError(
        1,
        0.2,
        ChunkedEncodingError(
            ProtocolError(
                "Connection broken",
                IncompleteRead(received, remaining),
            )
        ),
    )


def test_connection_failure_backs_off_and_retries(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    capture = object()
    memento = FakeMemento()
    client = FakeClient([_connection_refused_retry_error(), memento])

    retrieval.retrieve_response(client, capture)

    assert delays == [2]
    assert [call[0] for call in client.calls] == [capture, capture]


def test_one_workers_backoff_does_not_pause_another(monkeypatch):
    sleeping = threading.Event()
    release = threading.Event()

    def blocking_sleep(_seconds):
        sleeping.set()
        release.wait(timeout=2)

    monkeypatch.setattr(retrieval, "sleep_seconds", blocking_sleep)
    failing = FakeClient(
        [_connection_refused_retry_error(), FakeMemento()]
    )
    healthy = FakeClient([FakeMemento(content=b"healthy")])

    with ThreadPoolExecutor(max_workers=2) as executor:
        recovering = executor.submit(
            retrieval.retrieve_memento,
            failing,
            object(),
            retries=1,
        )
        assert sleeping.wait(timeout=1)
        unaffected = executor.submit(
            retrieval.retrieve_memento,
            healthy,
            object(),
            retries=1,
        )
        assert unaffected.result(timeout=1).body == b"healthy"
        assert not recovering.done()
        release.set()
        assert recovering.result(timeout=1).body == b"semantic payload"


def test_sustained_connection_failure_exhausts_bounded_attempts(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            _connection_refused_retry_error()
            for _ in range(3)
        ]
    )

    with pytest.raises(retry.RetryExhaustedError):
        retrieval.retrieve_response(client, object(), retries=2)
    assert delays == [2, 4]


def test_timeout_wayback_retry_uses_bounded_retry(monkeypatch):
    _make_retries_immediate(monkeypatch)
    error = WaybackRetryError(0, 1.0, ReadTimeout("read timed out"))
    client = FakeClient([error, FakeMemento()])

    retrieval.retrieve_response(client, object())

    assert len(client.calls) == 2


def test_repeated_identical_incomplete_read_stops_early(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            _truncated_retry_error()
            for _ in range(retrieval.REPEATED_TRUNCATION_ATTEMPTS)
        ]
        + [FakeMemento()]
    )

    with pytest.raises(
        retrieval.TruncatedWaybackResponseError
    ) as raised:
        retrieval.retrieve_response(client, object())

    error = raised.value
    assert len(client.calls) == retrieval.REPEATED_TRUNCATION_ATTEMPTS
    assert delays == [2, 4]
    assert error.received_bytes == 130810
    assert error.expected_bytes == 275029
    assert error.attempts == retrieval.REPEATED_TRUNCATION_ATTEMPTS
    assert (
        "truncated Wayback response after 3 attempts over "
        in str(error)
    )
    assert "received 130,810 of 275,029 bytes" in str(error)


def test_changing_incomplete_read_boundaries_keep_retrying(monkeypatch):
    delays = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            _truncated_retry_error(received=100, remaining=200),
            _truncated_retry_error(received=125, remaining=175),
            FakeMemento(content=b"recovered"),
        ]
    )

    response = retrieval.retrieve_response(client, object())

    assert response.content_stream().read() == b"recovered"
    assert len(client.calls) == 3
    assert delays == [2, 4]


def test_content_decoding_error_retries_once_with_identity_without_throttle():
    capture = object()
    broken = BrokenEncodingMemento(
        ContentDecodingError("incorrect gzip header")
    )
    client = FakeClient([broken, FakeMemento(content=b"recovered")])
    client.session = type(
        "Session",
        (),
        {
            "headers": {"Accept-Encoding": "gzip, deflate"},
            "reset_count": 0,
            "reset": lambda self: setattr(
                self,
                "reset_count",
                self.reset_count + 1,
            ),
        },
    )()
    encodings = []
    original_get = client.get_memento

    def get_memento(selected, **kwargs):
        encodings.append(client.session.headers["Accept-Encoding"])
        return original_get(selected, **kwargs)

    client.get_memento = get_memento
    response = retrieval.retrieve_response(
        client,
        capture,
    )

    assert response.content_stream().read() == b"recovered"
    assert broken.closed is True
    assert encodings == ["gzip, deflate", "identity"]
    assert client.session.headers["Accept-Encoding"] == "gzip, deflate"
    assert client.session.reset_count == 1


def test_identity_decoding_failure_skips_immediately_and_restores_header():
    first = BrokenEncodingMemento(
        ContentDecodingError("incorrect gzip header")
    )
    second = BrokenEncodingMemento(
        ContentDecodingError("still incorrect")
    )
    client = FakeClient([first, second])
    client.session = type(
        "Session",
        (),
        {
            "headers": {"Accept-Encoding": "gzip, deflate"},
            "reset": lambda self: None,
        },
    )()
    with pytest.raises(
        retrieval.MalformedContentEncodingError,
        match=(
            "original Wayback replay could not be decoded "
            "by the HTTP client"
        ),
    ) as raised:
        retrieval.retrieve_response(client, object())

    assert len(client.calls) == 2
    assert client.session.headers["Accept-Encoding"] == "gzip, deflate"
    assert (
        "retrying with Accept-Encoding: identity also failed"
        in str(raised.value)
    )
    assert "(Content-Encoding: gzip)" in str(raised.value)
    assert "still incorrect" in str(raised.value)


def test_make_client_factory_uses_application_owned_session(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeWaybackClient:
        def __init__(self, session=None):
            self.session = session

    monkeypatch.setattr(
        retrieval,
        "ArchiveMagicWaybackSession",
        FakeSession,
    )
    monkeypatch.setattr(retrieval, "WaybackClient", FakeWaybackClient)

    client = retrieval.make_client_factory("test-agent")()
    assert captured["user_agent"] == "test-agent"
    assert client.session is not None


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
