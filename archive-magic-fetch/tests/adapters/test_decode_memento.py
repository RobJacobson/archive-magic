"""Behavioral coverage for adapters / test_wayback_decoding."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from archive_magic_fetch.adapters.interpret_wayback_failures import (
    ExactMismatchError,
    classify_playback_error,
)
from archive_magic_fetch.archive.identity import payload_digest
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.models import (
    CaptureListing,
    CaptureRef,
    FailureCategory,
)
from archive_magic_fetch.pipeline.decoding.decode_memento import decode_memento
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from helpers import (
    fetch_memento,
    found_capture_client,
    make_capt,
    memento_client,
)
from wayback.exceptions import MementoPlaybackError


@pytest.mark.parametrize(
    "failure", [None, ValueError("read failed"), KeyboardInterrupt(), "mismatch"]
)
def test_memento_closed_after_read_or_validation_failure(failure):
    identity = make_capt(digest=payload_digest(b"hello"))
    closed = []

    class Memento:
        status_code = 200
        memento_url = "fixture://memento"
        timestamp = datetime(2004, 6, 15, tzinfo=timezone.utc)
        headers = {"Content-Type": "text/html"}
        url = (
            "http://example.org/other"
            if failure == "mismatch"
            else identity.original_url
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        @property
        def content(self):
            if isinstance(failure, BaseException):
                raise failure
            return b"hello"

    if failure is None:
        assert (
            decode_memento(Memento(), CaptureRef(identity, "text/html")).body
            == b"hello"
        )
    else:
        expected = ExactMismatchError if failure == "mismatch" else type(failure)
        with pytest.raises(expected):
            decode_memento(Memento(), CaptureRef(identity, "text/html"))
    assert closed == [True]


def test_decoding_timeout_retries_the_complete_attempt_and_closes_both_responses(
    tmp_path,
):
    from archive_magic_fetch.adapters.build_wayback_source import build_source
    from requests import ReadTimeout

    capture = CaptureRef(make_capt(digest=payload_digest(b"hello")), "text/html")
    attempts, responses_closed, clients_closed, sleeps = [], [], [], []

    class Memento:
        status_code = 200
        timestamp = datetime(2004, 6, 15, tzinfo=timezone.utc)
        url = capture.identity.original_url
        memento_url = "fixture://memento"
        headers = {"Content-Type": "text/html"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            responses_closed.append(self)

        @property
        def content(self):
            if len(attempts) == 1:
                raise ReadTimeout("reading the Memento timed out")
            return b"hello"

    class Client:
        def get_memento(self, url, **kwargs):
            attempts.append((url, kwargs))
            return Memento()

    @contextmanager
    def open_client(stats):
        client = Client()
        try:
            yield client
        finally:
            clients_closed.append(client)

    source = replace(
        build_source(index_directory=tmp_path / "index"),
        discover=lambda request, stats: CaptureListing((capture,), {}),
        open_client=open_client,
    )
    settings = FetchSettings(
        "example.org",
        "20040101000000",
        "20041231235959",
        "example.org",
        FetchOutput("local", tmp_path / "data"),
        playback_workers=1,
    )
    result = run_fetch(settings, source=source, sleep=sleeps.append)
    assert result.metrics.downloads == 1
    assert result.metrics.playback_attempts == 2
    assert sleeps == [5.0]
    assert len(attempts) == len(responses_closed) == 2
    assert len(clients_closed) == 1
    assert attempts[0] == attempts[1]


@pytest.mark.parametrize("status", ["200", "301"])
def test_nearby_capture_is_not_fetched_or_reconstructed(status):
    identity = make_capt(
        url="http://example.org/groups",
        ts="20041009172745",
        status=status,
    )
    client = found_capture_client(
        "http://example.org/groups/",
        "20041009202542",
        b"nearby content",
    )
    with pytest.raises(MementoPlaybackError):
        fetch_memento(client, identity)
    assert client.calls == 1


def test_download_exact_accepts_ia_double_encoded_original_url():
    """IA Link rel=original may %25-escape already-encoded query bytes."""

    from archive_magic_fetch.adapters.interpret_wayback_failures import ExactMismatchError
    from helpers import fetch_memento

    cdx_url = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%20DESC&state=46"
    )
    link_url = (
        "http://lideres.nclr.org/groups/index.php?view=browse"
        "&PHPSESSID=abc&page=5&sort=name%2520DESC&state=46"
    )
    identity = make_capt(url=cdx_url, ts="20041116040449")
    body = b"<html>ok</html>"
    result = fetch_memento(
        memento_client(identity, body, returned_url=link_url),
        identity,
    )
    assert result.identity.original_url == cdx_url
    assert result.body == body
    mismatch = make_capt(url="http://example.org/a")
    with pytest.raises(ExactMismatchError, match="URL mismatch"):
        fetch_memento(
            memento_client(
                mismatch,
                b"x",
                returned_url="http://example.org/b",
            ),
            mismatch,
        )


def test_cdx_digest_matches_body_accepts_trailing_newline_soft_match():
    from archive_magic_fetch.archive.identity import payload_digest
    from archive_magic_fetch.pipeline.decoding.decode_memento import _cdx_digest_matches_body
    from helpers import fetch_memento

    body = b"GIF89a-soft-match"
    exact = payload_digest(body)
    soft = payload_digest(body + b"\n")
    other = payload_digest(b"different")

    assert _cdx_digest_matches_body(exact, body) is True
    assert _cdx_digest_matches_body(soft, body) is True
    assert _cdx_digest_matches_body(other, body) is False
    assert _cdx_digest_matches_body(None, body) is True

    identity = make_capt(digest=soft)
    result = fetch_memento(memento_client(identity, body), identity)
    assert result.digest_matched is True
    assert result.body == body
    assert result.warc_payload_digest == exact
    assert result.warc_payload_digest != soft


def test_empty_non_redirect_playback_is_rejected_when_cdx_digest_is_nonempty():
    from archive_magic_fetch.adapters.interpret_wayback_failures import UnusablePlaybackError
    from helpers import fetch_memento

    identity = make_capt(status="200")
    with pytest.raises(UnusablePlaybackError, match="empty playback body"):
        fetch_memento(memento_client(identity, b""), identity)


def test_empty_http_200_matching_cdx_digest_is_stored():
    from archive_magic_fetch.archive.format import EMPTY_PAYLOAD_DIGEST
    from helpers import fetch_memento

    identity = make_capt(status="200", digest=EMPTY_PAYLOAD_DIGEST)
    result = fetch_memento(memento_client(identity, b""), identity)
    assert result.body == b""
    assert result.status_code == 200
    assert result.digest_matched is True
    assert result.warc_payload_digest == EMPTY_PAYLOAD_DIGEST


def test_invalid_uri_playback_is_always_rejected():
    from archive_magic_fetch.adapters.interpret_wayback_failures import (
        UnusablePlaybackError,
        classify_playback_error,
    )
    from helpers import fetch_memento

    identity = make_capt()
    with pytest.raises(UnusablePlaybackError):
        fetch_memento(memento_client(identity, b"Invalid URI"), identity)
    category, retryable = classify_playback_error(
        UnusablePlaybackError("IA playback stub: Invalid URI")
    )
    assert category == FailureCategory.UNAVAILABLE
    assert retryable is False


def test_playback_5xx_is_retryable():
    category, retryable = classify_playback_error(
        MementoPlaybackError("500 error while loading memento at http://x")
    )
    assert retryable is True
    assert category == FailureCategory.RETRY_EXHAUSTED


def test_wrapped_incomplete_read_is_permanent_truncated_failure():
    from requests.exceptions import ChunkedEncodingError
    from wayback.exceptions import WaybackRetryError

    incomplete = ChunkedEncodingError(
        "Connection broken: IncompleteRead(130810 bytes read, 292753 more expected)"
    )
    wrapped = WaybackRetryError(0, 0.08, incomplete)

    category, retryable = classify_playback_error(wrapped)

    assert category == FailureCategory.TRUNCATED
    assert retryable is False
