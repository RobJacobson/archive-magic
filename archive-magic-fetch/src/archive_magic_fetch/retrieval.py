"""Wayback Memento retrieval and semantic WARC response construction."""

from __future__ import annotations

import time
from http import HTTPStatus
from io import BytesIO
from typing import Mapping

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


def retrieve_response(client, capture):
    """Retrieve one Memento and construct the semantic WARC response."""

    memento = _get_memento_with_retry(client, capture)
    with memento:
        payload = memento.content
        url = memento.url
        capture_date = timestamp_to_warc_date(memento.timestamp)
        source_uri = memento.memento_url
        status_code = memento.status_code
        headers = _semantic_headers(memento.headers, len(payload))

        http_headers = StatusAndHeaders(
            _status_line(status_code),
            headers,
            protocol="HTTP/1.1",
        )
        builder = RecordBuilder(warc_version="1.0")
        return builder.create_warc_record(
            url,
            "response",
            payload=BytesIO(payload),
            length=len(payload),
            http_headers=http_headers,
            warc_headers_dict={
                "WARC-Date": capture_date,
                "WARC-Source-URI": source_uri,
            },
        )
