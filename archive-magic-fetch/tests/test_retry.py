import pytest
from requests.exceptions import ConnectionError
from wayback import WaybackSession
from wayback.exceptions import WaybackRetryError

from archive_magic_fetch import retry


def test_retry_delay_is_exponential_and_honors_retry_after():
    assert retry.DEFAULT_RETRIES == 8
    assert retry.retry_delay_seconds(1) == 10
    assert retry.retry_delay_seconds(8) == 1280
    assert retry.retry_delay_seconds(3, retry_after=41) == 41
    assert retry.retry_delay_seconds(100) == 5 * (2**100)


def test_sleep_seconds_chunks_platform_sized_waits(monkeypatch):
    sleeps = []
    monkeypatch.setattr(retry.time, "sleep", sleeps.append)

    retry.sleep_seconds(7201)

    assert sleeps == [3600, 3600, 1]


def test_retry_decision_unwraps_disabled_library_retry():
    cause = ConnectionError("connection refused")
    error = WaybackRetryError(0, 0.1, cause)

    decision = retry.retry_decision(error)

    assert decision is not None
    assert decision.cause is cause


class FakeResponse:
    def __init__(self, *, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.url = "https://web.archive.org/test"
        self.closed = False

    def close(self):
        self.closed = True


def test_application_session_exposes_retryable_service_response(monkeypatch):
    sent = {}
    response = FakeResponse(
        status_code=503,
        headers={"Retry-After": "17"},
    )

    def send(_self, _request, **kwargs):
        sent.update(kwargs)
        return response

    monkeypatch.setattr(
        WaybackSession,
        "send",
        send,
    )
    session = retry.ArchiveMagicWaybackSession()

    with pytest.raises(
        retry.RetryableWaybackResponseError
    ) as raised:
        session.send(object())

    assert raised.value.status_code == 503
    assert raised.value.retry_after == 17
    assert response.closed is True
    assert session.retries == 0
    assert sent["stream"] is True


def test_application_session_preserves_historical_503_memento(monkeypatch):
    response = FakeResponse(
        status_code=503,
        headers={"Memento-Datetime": "Thu, 01 Jan 1970 00:00:00 GMT"},
    )
    monkeypatch.setattr(
        WaybackSession,
        "send",
        lambda _self, _request, **_kwargs: response,
    )
    session = retry.ArchiveMagicWaybackSession()

    assert session.send(object()) is response
    assert response.closed is False
