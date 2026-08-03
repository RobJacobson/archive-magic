"""Download and validate Wayback captures."""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.client import IncompleteRead as HttpIncompleteRead
from io import BytesIO
from typing import Callable, Mapping, Optional

from requests.exceptions import ContentDecodingError, RequestException
from urllib3.exceptions import (
    HTTPError as Urllib3HTTPError,
    IncompleteRead as Urllib3IncompleteRead,
)
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from wayback import Mode, WaybackClient
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    RateLimitError,
    WaybackRetryError,
)

from .console import print_progress
from .retry import (
    DEFAULT_RETRIES,
    ArchiveMagicWaybackSession,
    RetryExhaustedError,
    RetryableWaybackResponseError,
    format_seconds,
    retry_decision,
    retry_delay_seconds,
    sleep_seconds,
)
from .warc_records import timestamp_to_warc_date


DEFAULT_WORKER_COUNT = 8

PLAYBACK_ERRORS = (
    MementoPlaybackError,
    RetryExhaustedError,
    BlockedByRobotsError,
    BlockedSiteError,
    WaybackRetryError,
)

REPEATED_TRUNCATION_ATTEMPTS = 2

_VALID_CDX_SHA1 = re.compile(r"[A-Z2-7]{32}")

_REPRESENTATION_HEADERS = {
    "content-digest",
    "content-encoding",
    "content-length",
    "content-md5",
    "content-range",
    "digest",
    "etag",
    "repr-digest",
    "transfer-encoding",
}


class ThreadClientPool:
    """Lazily own and reuse one Wayback client per worker thread."""

    def __init__(self, factory: Callable) -> None:
        self._factory = factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._clients: list[object] = []

    def get(self):
        active = getattr(self._local, "active", None)
        if active is not None:
            return active

        client = self._factory()
        enter = getattr(client, "__enter__", None)
        active = enter() if callable(enter) else client
        if active is None:
            active = client
        self._local.active = active
        with self._lock:
            self._clients.append(client)
        return active

    def close(self) -> None:
        """Close every client after all worker threads have stopped."""

        for client in self._clients:
            exit_fn = getattr(client, "__exit__", None)
            if callable(exit_fn):
                exit_fn(None, None, None)
            else:
                close = getattr(client, "close", None)
                if callable(close):
                    close()


class MalformedContentEncodingError(MementoPlaybackError):
    """The HTTP client could not decode an original Wayback replay."""

    def __init__(
        self,
        encoding: Optional[str] = None,
        *,
        cause: Optional[Exception] = None,
    ) -> None:
        self.encoding = encoding
        self.cause = cause
        if encoding:
            encoding_detail = f" (Content-Encoding: {encoding})"
        else:
            encoding_detail = ""
        cause_text = str(cause).strip() if cause is not None else ""
        cause_detail = f": {cause_text}" if cause_text else ""
        super().__init__(
            "original Wayback replay could not be decoded by the HTTP client"
            f"{encoding_detail}{cause_detail}; raw recovery was not verified "
            "by the CDX digest, so the capture was discarded"
        )


class TruncatedWaybackResponseError(MementoPlaybackError):
    """Wayback repeatedly stopped at the same incomplete response boundary."""

    def __init__(
        self,
        *,
        received_bytes: int,
        expected_bytes: int,
        attempts: int,
        elapsed_seconds: float,
    ) -> None:
        self.received_bytes = received_bytes
        self.expected_bytes = expected_bytes
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            "truncated Wayback response after "
            f"{attempts} attempts over {elapsed_seconds:.1f}s "
            f"(received {received_bytes:,} of {expected_bytes:,} bytes)"
        )


def format_playback_failure(error: Exception) -> str:
    """Return one concise user-facing playback failure reason."""

    if isinstance(
        error,
        (
            MalformedContentEncodingError,
            RetryExhaustedError,
            TruncatedWaybackResponseError,
        ),
    ):
        return str(error)
    if isinstance(error, WaybackRetryError):
        elapsed = (
            f"{float(error.time):.1f}s"
            if isinstance(error.time, (int, float))
            else "an unknown duration"
        )
        attempts = "attempt" if error.retries == 1 else "attempts"
        return (
            f"Wayback request failed after {error.retries} {attempts} over "
            f"{elapsed}: {error.cause}"
        )
    return str(error) or type(error).__name__


def _content_encoding(memento) -> Optional[str]:
    """Return the replay response encoding involved in a decode failure."""

    for attribute in ("_raw_headers", "raw_headers", "headers"):
        headers = getattr(memento, attribute, None)
        if headers is None:
            continue
        value = headers.get("Content-Encoding")
        if value is not None:
            encoding = str(value).strip()
            if encoding:
                return encoding
    return None


def normalize_cdx_digest(digest: object) -> Optional[str]:
    """Return a canonical CDX SHA-1 payload digest when valid."""

    if not isinstance(digest, str):
        return None
    value = digest.strip().upper()
    if value.startswith("SHA1:"):
        value = value[5:]
    if not _VALID_CDX_SHA1.fullmatch(value):
        return None
    return f"sha1:{value}"


def _payload_digest(payload: bytes) -> str:
    """Return one CDX-compatible SHA-1 digest for semantic payload bytes."""

    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode(
        "ascii"
    )
    return f"sha1:{encoded}"


def _recover_raw_payload(
    client,
    capture,
    *,
    source_uri: str,
    status_code: int,
) -> Optional[bytes]:
    """Return one exact raw replay only when its CDX digest verifies it."""

    expected_digest = normalize_cdx_digest(
        getattr(capture, "digest", None)
    )
    if expected_digest is None:
        return None

    try:
        with client.session.request(
            "GET",
            source_uri,
            allow_redirects=False,
        ) as response:
            if (
                response.status_code != status_code
                or "Memento-Datetime" not in response.headers
            ):
                return None
            payload = response.raw.read(decode_content=False)
    except (
        RateLimitError,
        RetryableWaybackResponseError,
        WaybackRetryError,
        RequestException,
        Urllib3HTTPError,
    ):
        return None

    if _payload_digest(payload) != expected_digest:
        return None
    return payload


def _incomplete_read_boundary(
    error: BaseException,
) -> Optional[tuple[int, int]]:
    """Find a structured IncompleteRead boundary in nested request errors."""

    pending: list[object] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)

        if isinstance(
            current,
            (HttpIncompleteRead, Urllib3IncompleteRead),
        ):
            partial = current.partial
            expected = current.expected
            received = (
                len(partial)
                if isinstance(partial, (bytes, bytearray))
                else partial
            )
            if (
                isinstance(received, int)
                and isinstance(expected, int)
                and received >= 0
                and expected >= 0
            ):
                return received, received + expected
            return None

        if isinstance(current, BaseException):
            pending.extend(current.args)
            for attribute in ("cause", "__cause__", "__context__"):
                nested = getattr(current, attribute, None)
                if nested is not None:
                    pending.append(nested)
        elif isinstance(current, (tuple, list)):
            pending.extend(current)
    return None


@dataclass(frozen=True)
class DownloadedCapture:
    """Semantic playback result reusable by WARC and loose-file writers."""

    body: bytes
    url: str
    capture_date: str
    source_uri: str
    status_code: int
    headers: tuple[tuple[str, str], ...]

    def to_warc_record(self, *, target_url: Optional[str] = None):
        """Build a fresh WARC response record over the semantic body."""

        http_headers = StatusAndHeaders(
            _status_line(self.status_code),
            list(self.headers),
            protocol="HTTP/1.1",
        )
        builder = RecordBuilder(warc_version="1.0")
        return builder.create_warc_record(
            target_url or self.url,
            "response",
            payload=BytesIO(self.body),
            length=len(self.body),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Date": self.capture_date,
                "WARC-Source-URI": self.source_uri,
            },
        )


def make_client_factory(user_agent: str) -> Callable[[], WaybackClient]:
    """Return a factory of Wayback clients that share default rate limits.

    Each call creates a fresh session with library retries disabled.
    Unspecified rate limits remain shared process-wide and thread-safe.
    """

    def factory() -> WaybackClient:
        return WaybackClient(
            session=ArchiveMagicWaybackSession(
                user_agent=user_agent,
            )
        )

    return factory


def _download_capture_with_retry(
    client,
    capture,
    *,
    retries: int,
) -> DownloadedCapture:
    """Retrieve and consume one Memento with application-owned retries."""

    started_at = time.monotonic()
    attempt_number = 0
    previous_truncation = None
    repeated_truncations = 0
    capture_label = getattr(capture, "view_url", str(capture))
    while attempt_number <= retries:
        attempt_number += 1
        memento = None
        try:
            memento = client.get_memento(
                capture,
                mode=Mode.original,
                exact=True,
                follow_redirects=False,
            )
            with memento:
                payload = memento.content
                headers = tuple(
                    _semantic_headers(memento.headers, len(payload))
                )
                result = DownloadedCapture(
                    body=payload,
                    url=memento.url,
                    capture_date=timestamp_to_warc_date(
                        memento.timestamp
                    ),
                    source_uri=memento.memento_url,
                    status_code=memento.status_code,
                    headers=headers,
                )
        except ContentDecodingError as error:
            payload = (
                _recover_raw_payload(
                    client,
                    capture,
                    source_uri=memento.memento_url,
                    status_code=memento.status_code,
                )
                if memento is not None
                else None
            )
            if payload is None:
                raise MalformedContentEncodingError(
                    _content_encoding(memento),
                    cause=error,
                ) from error
            return DownloadedCapture(
                body=payload,
                url=memento.url,
                capture_date=timestamp_to_warc_date(memento.timestamp),
                source_uri=memento.memento_url,
                status_code=memento.status_code,
                headers=tuple(
                    _semantic_headers(memento.headers, len(payload))
                ),
            )
        except (
            RateLimitError,
            RetryableWaybackResponseError,
            WaybackRetryError,
            RequestException,
        ) as error:
            decision = retry_decision(error)
            if decision is None:
                raise
            truncation = _incomplete_read_boundary(error)
            if truncation is not None:
                if truncation == previous_truncation:
                    repeated_truncations += 1
                else:
                    previous_truncation = truncation
                    repeated_truncations = 1
            else:
                previous_truncation = None
                repeated_truncations = 0

            repeated_boundary = (
                truncation is not None
                and repeated_truncations
                >= REPEATED_TRUNCATION_ATTEMPTS
            )
            if repeated_boundary:
                received, expected = truncation
                raise TruncatedWaybackResponseError(
                    received_bytes=received,
                    expected_bytes=expected,
                    attempts=attempt_number,
                    elapsed_seconds=time.monotonic() - started_at,
                ) from error

            if attempt_number > retries:
                if truncation is not None:
                    received, expected = truncation
                    raise TruncatedWaybackResponseError(
                        received_bytes=received,
                        expected_bytes=expected,
                        attempts=attempt_number,
                        elapsed_seconds=time.monotonic() - started_at,
                    ) from error
                raise RetryExhaustedError(
                    attempts=attempt_number,
                    elapsed_seconds=time.monotonic() - started_at,
                    cause=decision.cause,
                ) from error

            if truncation is not None:
                print_progress(
                    f"{capture_label}: retrying after incomplete response"
                )
                continue

            delay = retry_delay_seconds(
                attempt_number,
                retry_after=decision.retry_after,
            )
            print_progress(
                f"{capture_label}: retry {attempt_number}/{retries} in "
                f"{format_seconds(delay)}s after {decision.cause}"
            )
            sleep_seconds(delay)
        else:
            return result

    raise RuntimeError("unreachable memento retry state")  # pragma: no cover


def _semantic_headers(
    headers: Mapping[str, str],
    payload_length: int,
) -> list[tuple[str, str]]:
    """Return historical headers consistent with the semantic payload."""

    semantic = [
        (name, value)
        for name, value in headers.items()
        if name.lower() not in _REPRESENTATION_HEADERS
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic


def _status_line(status_code: int) -> str:
    """Return a standard HTTP status line without inventing unknown reasons."""

    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def download_capture(
    client,
    capture,
    *,
    retries: int = DEFAULT_RETRIES,
) -> DownloadedCapture:
    """Retrieve one Memento as reusable semantic body and metadata."""

    if retries < 0:
        raise ValueError("retries cannot be negative")
    return _download_capture_with_retry(
        client,
        capture,
        retries=retries,
    )


def download_response(
    client,
    capture,
    *,
    retries: int = DEFAULT_RETRIES,
):
    """Retrieve one Memento and construct the semantic WARC response."""

    return download_capture(
        client,
        capture,
        retries=retries,
    ).to_warc_record()
