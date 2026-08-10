"""Wayback session edge cases for IA mementos and CDX."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock

import pytest
from urllib3 import HTTPResponse

from helpers import make_capt  # noqa: F401
def _urllib3_response(body: bytes, headers: dict[str, str]):
    return HTTPResponse(
        body=BytesIO(body),
        headers=headers,
        status=200,
        preload_content=False,
        decode_content=False,
        original_response=None,
    )


def _requests_response(body: bytes, *, content_encoding: str | None, memento: bool):
    from requests import Response

    headers: dict[str, str] = {}
    if content_encoding is not None:
        headers["Content-Encoding"] = content_encoding
    if memento:
        headers["Memento-Datetime"] = "Fri, 09 May 2008 08:22:33 GMT"
    response = Response()
    response.status_code = 200
    response.url = "https://web.archive.org/web/20080509082233id_/http://example.org/"
    response.headers.update(headers)
    response.raw = _urllib3_response(body, headers)
    return response




def test_session_raises_rate_limit_for_429_memento_response():
    from archive_magic_fetch.cdx import ArchiveMagicWaybackSession
    from wayback import WaybackSession
    from wayback.exceptions import RateLimitError

    session = ArchiveMagicWaybackSession()
    response = MagicMock()
    response.status_code = 429
    response.headers = {
        "Memento-Datetime": "Wed, 01 Jun 2004 00:00:00 GMT",
        "Retry-After": "17",
    }
    # Parent would treat this as a successful memento; our session must not.

    original_send = WaybackSession.send

    def fake_send(self, request, **kwargs):
        return response

    WaybackSession.send = fake_send  # type: ignore[method-assign]
    try:
        with pytest.raises(RateLimitError) as raised:
            session.send(MagicMock())
        assert raised.value.retry_after == 17
    finally:
        WaybackSession.send = original_send  # type: ignore[method-assign]
        session.close()


def test_session_repairs_false_gzip_content_encoding_on_mementos():
    """IA may claim gzip while the memento body is already plaintext HTML."""

    import gzip as gzip_mod

    from archive_magic_fetch.cdx import ArchiveMagicWaybackSession
    from wayback import WaybackSession

    plaintext = b"<!DOCTYPE html><html><body>ok</body></html>"
    real_gzip = gzip_mod.compress(b"compressed-payload")
    session = ArchiveMagicWaybackSession()
    original_send = WaybackSession.send

    def run(canned, expected):
        def fake_send(self, request, **kwargs):
            return canned

        WaybackSession.send = fake_send  # type: ignore[method-assign]
        repaired = session.send(MagicMock())
        assert repaired.content == expected
        assert "Content-Encoding" not in repaired.headers

    try:
        # False CE:gzip — body is HTML; must not raise ContentDecodingError.
        run(
            _requests_response(plaintext, content_encoding="gzip", memento=True),
            plaintext,
        )
        # True CE:gzip — still expose the decompressed payload.
        run(
            _requests_response(real_gzip, content_encoding="gzip", memento=True),
            b"compressed-payload",
        )
    finally:
        WaybackSession.send = original_send  # type: ignore[method-assign]
        session.close()


def test_session_leaves_non_memento_gzip_bodies_unconsumed():
    """CDX responses must keep streaming; do not eagerly rewrite them."""

    from archive_magic_fetch.cdx import repair_false_gzip_content_encoding

    body = b'[["urlkey","timestamp"]]'
    response = _requests_response(body, content_encoding="gzip", memento=False)
    repair_false_gzip_content_encoding(response)
    assert response._content is False
    assert response.headers.get("Content-Encoding") == "gzip"
    # Stream still available for decode_content=False CDX readers.
    assert response.raw.read() == body


