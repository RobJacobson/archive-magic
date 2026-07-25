"""Wayback Memento retrieval and semantic WARC response construction."""

from __future__ import annotations

import time
from dataclasses import dataclass
from http import HTTPStatus
from io import BytesIO
from typing import Mapping, Optional

from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from wayback import Mode
from wayback.exceptions import RateLimitError

from .warc import timestamp_to_warc_date


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


@dataclass(frozen=True)
class RetrievedMemento:
    """Semantic playback result reusable by WARC and loose-file writers."""

    body: bytes
    url: str
    capture_date: str
    source_uri: str
    status_code: int
    headers: tuple[tuple[str, str], ...]

    def to_warc_record(self):
        """Build a fresh WARC response record over the semantic body."""

        http_headers = StatusAndHeaders(
            _status_line(self.status_code),
            list(self.headers),
            protocol="HTTP/1.1",
        )
        builder = RecordBuilder(warc_version="1.0")
        return builder.create_warc_record(
            self.url,
            "response",
            payload=BytesIO(self.body),
            length=len(self.body),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Date": self.capture_date,
                "WARC-Source-URI": self.source_uri,
            },
        )


class RetrievalCache:
    """Fetch each distinct capture once and fan out to multiple writers."""

    def __init__(self) -> None:
        self._results: dict[tuple[object, ...], object] = {}

    @staticmethod
    def _key(capture) -> tuple[object, ...]:
        return (
            capture.urlkey,
            capture.original,
            capture.timestamp,
            capture.statuscode,
            capture.digest,
        )

    def retrieve(self, client, capture) -> RetrievedMemento:
        """Return a cached memento or retrieve and remember it."""

        key = self._key(capture)
        cached = self._results.get(key)
        if cached is not None:
            if isinstance(cached, BaseException):
                raise cached
            return cached

        try:
            result = retrieve_memento(client, capture)
        except BaseException as error:
            self._results[key] = error
            raise
        self._results[key] = result
        return result


def _get_memento_with_retry(client, capture):
    """Retrieve one exact Memento, pausing once after a rate limit."""

    def attempt():
        return client.get_memento(
            capture,
            mode=Mode.original,
            exact=True,
            follow_redirects=False,
        )

    try:
        return attempt()
    except RateLimitError as error:
        time.sleep(error.retry_after or 60)
        return attempt()


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


def retrieve_memento(client, capture) -> RetrievedMemento:
    """Retrieve one Memento as reusable semantic body and metadata."""

    memento = _get_memento_with_retry(client, capture)
    with memento:
        payload = memento.content
        headers = tuple(
            _semantic_headers(memento.headers, len(payload))
        )
        return RetrievedMemento(
            body=payload,
            url=memento.url,
            capture_date=timestamp_to_warc_date(memento.timestamp),
            source_uri=memento.memento_url,
            status_code=memento.status_code,
            headers=headers,
        )


def retrieve_response(client, capture, *, cache: Optional[RetrievalCache] = None):
    """Retrieve one Memento and construct the semantic WARC response."""

    if cache is None:
        return retrieve_memento(client, capture).to_warc_record()
    return cache.retrieve(client, capture).to_warc_record()
