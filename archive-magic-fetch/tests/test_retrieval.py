import base64
import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from requests.exceptions import ChunkedEncodingError, ContentDecodingError
from urllib3.exceptions import IncompleteRead, ProtocolError
from wayback import Mode
from wayback.exceptions import (
    MementoPlaybackError,
    RateLimitError,
    UnexpectedResponseFormat,
    WaybackRetryError,
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
    rate_limit_delays = []
    transient_failures = []

    def pause(_self, seconds):
        rate_limit_delays.append(seconds)
        return True

    def backoff(failure_number):
        transient_failures.append(failure_number)
        return 0

    monkeypatch.setattr(retrieval.RateLimitCooldown, "wait", lambda _self: None)
    monkeypatch.setattr(retrieval.RateLimitCooldown, "pause", pause)
    monkeypatch.setattr(retrieval, "_transient_backoff_seconds", backoff)
    return rate_limit_delays, transient_failures


def test_rate_limit_coordinates_backoff_and_retries_same_capture(
    monkeypatch,
    capsys,
):
    rate_limit_delays, transient_failures = _make_retries_immediate(monkeypatch)
    capture = object()
    memento = FakeMemento()
    client = FakeClient([RateLimitError(None, 11), memento])

    retrieval.retrieve_response(client, capture)

    assert rate_limit_delays == [11]
    assert transient_failures == []
    assert [call[0] for call in client.calls] == [capture, capture]
    assert capsys.readouterr().out == (
        "Rate limited by Internet Archive during playback; "
        "pausing all downloads for 11s before retrying...\n"
    )


def test_missing_retry_after_uses_sixty_second_backoff(monkeypatch):
    rate_limit_delays, _ = _make_retries_immediate(monkeypatch)
    client = FakeClient([RateLimitError(None, None), FakeMemento()])

    retrieval.retrieve_response(client, object())

    assert rate_limit_delays == [60]


def test_repeated_rate_limit_exhausts_bounded_attempts(monkeypatch):
    rate_limit_delays, _ = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            RateLimitError(None, attempt)
            for attempt in range(1, retrieval.MAX_RETRIEVAL_ATTEMPTS + 1)
        ]
    )

    with pytest.raises(WaybackRetryError):
        retrieval.retrieve_response(client, object())

    assert rate_limit_delays == list(
        range(1, retrieval.MAX_RETRIEVAL_ATTEMPTS)
    )
    assert len(client.calls) == retrieval.MAX_RETRIEVAL_ATTEMPTS


def test_rate_limit_cooldown_blocks_until_pause_expires():
    cooldown = retrieval.RateLimitCooldown()
    assert cooldown.pause(0.05) is True
    started = time.monotonic()

    cooldown.wait()

    assert time.monotonic() - started >= 0.04


def test_rate_limit_cooldown_reports_only_the_start_of_a_shared_pause():
    cooldown = retrieval.RateLimitCooldown()

    assert cooldown.pause(0.05) is True
    assert cooldown.pause(0.06) is False


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
    _, transient_failures = _make_retries_immediate(monkeypatch)
    capture = object()
    memento = FakeMemento()
    client = FakeClient([_connection_refused_retry_error(), memento])

    retrieval.retrieve_response(client, capture)

    assert transient_failures == [1]
    assert [call[0] for call in client.calls] == [capture, capture]


def test_sustained_connection_failure_exhausts_bounded_attempts(monkeypatch):
    _, transient_failures = _make_retries_immediate(monkeypatch)
    client = FakeClient(
        [
            _connection_refused_retry_error()
            for _ in range(retrieval.MAX_RETRIEVAL_ATTEMPTS)
        ]
    )

    with pytest.raises(WaybackRetryError):
        retrieval.retrieve_response(client, object())
    assert transient_failures == list(
        range(1, retrieval.MAX_RETRIEVAL_ATTEMPTS)
    )


def test_timeout_wayback_retry_uses_bounded_retry(monkeypatch):
    _make_retries_immediate(monkeypatch)
    error = WaybackRetryError(2, 1.0, TimeoutError("read timed out"))
    client = FakeClient([error, FakeMemento()])

    retrieval.retrieve_response(client, object())

    assert len(client.calls) == 2


def test_repeated_identical_incomplete_read_stops_early(monkeypatch):
    _, transient_failures = _make_retries_immediate(monkeypatch)
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
    assert transient_failures == [1, 2]
    assert error.received_bytes == 130810
    assert error.expected_bytes == 275029
    assert error.attempts == retrieval.REPEATED_TRUNCATION_ATTEMPTS
    assert (
        "truncated Wayback response after 3 attempts over "
        in str(error)
    )
    assert "received 130,810 of 275,029 bytes" in str(error)


def test_changing_incomplete_read_boundaries_keep_retrying(monkeypatch):
    _, transient_failures = _make_retries_immediate(monkeypatch)
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
    assert transient_failures == [1, 2]


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
    cooldown = retrieval.RateLimitCooldown()

    response = retrieval.retrieve_response(
        client,
        capture,
        cooldown=cooldown,
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
    cooldown = retrieval.RateLimitCooldown()

    with pytest.raises(
        retrieval.MalformedContentEncodingError,
        match="invalid Wayback replay response",
    ) as raised:
        retrieval.retrieve_response(client, object(), cooldown=cooldown)

    assert len(client.calls) == 2
    assert client.session.headers["Accept-Encoding"] == "gzip, deflate"
    assert (
        "retrying with Accept-Encoding: identity also failed"
        in str(raised.value)
    )
    assert "Content-Encoding declares gzip" in str(raised.value)


def test_make_client_factory_uses_reduced_worker_retries(monkeypatch):
    captured = {}

    class FakeSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeWaybackClient:
        def __init__(self, session=None):
            self.session = session

    monkeypatch.setattr(retrieval, "WaybackSession", FakeSession)
    monkeypatch.setattr(retrieval, "WaybackClient", FakeWaybackClient)

    client = retrieval.make_client_factory("test-agent")()
    assert captured["user_agent"] == "test-agent"
    assert captured["retries"] == retrieval.WORKER_SESSION_RETRIES
    assert client.session is not None


def test_memento_fetch_pool_reuses_thread_clients_and_waits_in_order():
    captures = []
    for index in range(3):
        captures.append(
            type(
                "Capture",
                (),
                {
                    "urlkey": f"key{index}",
                    "original": f"https://example.com/{index}",
                    "timestamp": datetime(2020, 1, 1, tzinfo=timezone.utc),
                    "statuscode": 200,
                    "digest": "A" * 32,
                },
            )()
        )

    created_clients = []
    active = 0
    max_active = 0
    lock = threading.Lock()

    class FactoryClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_memento(self, capture, **kwargs):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return FakeMemento(
                    content=f"body-{id(capture)}".encode(),
                    url=capture.original,
                )
            finally:
                with lock:
                    active -= 1

    def factory():
        client = FactoryClient()
        created_clients.append(client)
        return client

    pool = retrieval.MementoFetchPool(
        cooldown=retrieval.RateLimitCooldown(),
        client_factory=factory,
        max_workers=2,
    )
    try:
        pool.submit(captures)
        for capture in captures:
            retrieved = pool.wait(capture)
            assert retrieved.body == f"body-{id(capture)}".encode()
    finally:
        pool.close()

    assert 1 <= len(created_clients) <= 2
    assert max_active == 2


def test_memento_fetch_pool_fetches_duplicate_capture_keys_independently():
    capture = type(
        "Capture",
        (),
        {
            "urlkey": "com,example)/",
            "original": "https://example.com/",
            "timestamp": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "statuscode": 200,
            "digest": "A" * 32,
        },
    )()
    calls = []

    class FactoryClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_memento(self, selected, **kwargs):
            calls.append(selected)
            return FakeMemento()

    pool = retrieval.MementoFetchPool(
        cooldown=retrieval.RateLimitCooldown(),
        client_factory=FactoryClient,
        max_workers=2,
    )
    try:
        pool.submit([capture, capture])
        pool.wait(capture)
        pool.wait(capture)
    finally:
        pool.close()

    assert len(calls) == 2


def test_memento_fetch_pool_reports_completion_before_ordered_wait():
    capture = type(
        "Capture",
        (),
        {
            "urlkey": "com,example)/",
            "original": "https://example.com/",
            "timestamp": datetime(2020, 1, 1, tzinfo=timezone.utc),
            "statuscode": 200,
            "digest": "A" * 32,
        },
    )()
    reported = threading.Event()
    completed = []

    class FactoryClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_memento(self, selected, **kwargs):
            return FakeMemento()

    def on_fetched(selected):
        completed.append(selected)
        reported.set()

    pool = retrieval.MementoFetchPool(
        cooldown=retrieval.RateLimitCooldown(),
        client_factory=FactoryClient,
        max_workers=2,
        on_fetched=on_fetched,
    )
    try:
        pool.submit([capture])
        assert reported.wait(timeout=1)
        assert completed == [capture]
        assert pool.wait(capture) is not None
    finally:
        pool.close()


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
