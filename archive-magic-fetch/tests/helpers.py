"""Shared fixtures and helpers for Archive Magic Fetch tests."""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import MagicMock

from archive_magic_fetch.models import CaptureIdentity, PlaybackResult, make_identity
from archive_magic_fetch.warc import payload_digest


def make_capt(
    url: str = "http://example.org/",
    ts: str = "20040615000000",
    status: str = "200",
    digest: str = "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    urlkey: Optional[str] = None,
) -> CaptureIdentity:
    return make_identity(
        original_url=url,
        timestamp=ts,
        status_token=status,
        payload_digest=digest,
        urlkey=urlkey,
    )


def playback(
    capt: CaptureIdentity,
    body: bytes = b"hello",
    status: int = 200,
) -> PlaybackResult:
    return PlaybackResult(
        identity=capt,
        body=body,
        status_code=status,
        headers=(("Content-Type", "text/html"), ("Content-Length", str(len(body)))),
        warc_date=(
            f"{capt.timestamp[0:4]}-{capt.timestamp[4:6]}-"
            f"{capt.timestamp[6:8]}T{capt.timestamp[8:10]}:"
            f"{capt.timestamp[10:12]}:{capt.timestamp[12:14]}Z"
        ),
        source_uri=(
            f"https://web.archive.org/web/{capt.timestamp}id_/{capt.original_url}"
        ),
        warc_payload_digest=payload_digest(body),
    )


def cdx_json(rows: list[list[str]]) -> bytes:
    return json.dumps(rows).encode("utf-8")


class FakeRaw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, decode_content: bool = False):
        return self._body


class FakeSession:
    """Minimal session returning scripted CDX responses."""

    def __init__(self, bodies: list[bytes], status: int = 200) -> None:
        self.bodies = list(bodies)
        self.status = status
        self.calls = 0

    def get(self, url, stream=True, timeout=120):
        self.calls += 1
        body = self.bodies.pop(0) if self.bodies else b"[]"
        response = MagicMock()
        response.status_code = self.status
        response.content = body
        response.encoding = "utf-8"
        response.headers = {"Content-Encoding": "identity"}
        response.raw = FakeRaw(body)
        response.raise_for_status = MagicMock()
        response.close = MagicMock()
        return response

    def close(self):
        return None


def patch_cdx(body: bytes):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_year_cdx

    def fake_fetch_year_cdx(layout, **kwargs):
        kwargs = dict(kwargs)
        kwargs["session"] = FakeSession([body])
        kwargs["sleep"] = lambda _s: None
        return original(layout, **kwargs)

    cdx_mod.fetch_year_cdx = fake_fetch_year_cdx
    fetch_mod.fetch_year_cdx = fake_fetch_year_cdx
    return original, cdx_mod, fetch_mod


def patch_cdx_by_year(bodies_by_year: dict[int, bytes]):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_year_cdx

    def fake_fetch_year_cdx(layout, **kwargs):
        kwargs = dict(kwargs)
        year = int(kwargs["year"])
        body = bodies_by_year.get(year, b"[]")
        kwargs["session"] = FakeSession([body])
        kwargs["sleep"] = lambda _s: None
        return original(layout, **kwargs)

    cdx_mod.fetch_year_cdx = fake_fetch_year_cdx
    fetch_mod.fetch_year_cdx = fake_fetch_year_cdx
    return original, cdx_mod, fetch_mod


def memento_client(identity, body: bytes, *, headers: dict | None = None):
    from datetime import datetime, timezone

    class Client:
        def get_memento(self, *args, **kwargs):
            memento = MagicMock()
            memento.__enter__ = lambda s: s
            memento.__exit__ = MagicMock(return_value=False)
            memento.content = body
            if identity.status_token.isdigit():
                memento.status_code = int(identity.status_token)
            else:
                memento.status_code = 200
            memento.memento_url = (
                f"https://web.archive.org/web/{identity.timestamp}id_/"
                f"{identity.original_url}"
            )
            ts = identity.timestamp
            memento.timestamp = datetime(
                int(ts[0:4]),
                int(ts[4:6]),
                int(ts[6:8]),
                int(ts[8:10]),
                int(ts[10:12]),
                int(ts[12:14]),
                tzinfo=timezone.utc,
            )
            memento.headers = {"Content-Type": "text/html", **(headers or {})}
            memento.url = identity.original_url
            return memento

    return Client()
