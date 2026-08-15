"""Shared fixtures and helpers for Archive Magic Fetch tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock
from wayback import CdxRecord

from archive_magic_fetch.identity import make_identity
from archive_magic_fetch.models import CaptureIdentity, PlaybackResult
from archive_magic_fetch.playback import payload_digest


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


class FakeCdxClient:
    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = rows
        self.closed = False

    def search(self, *args, **kwargs):
        for row in self.rows:
            if not isinstance(row, list) or len(row) < 7:
                continue
            urlkey, timestamp, original, mimetype, status, digest, length = row[:7]
            yield CdxRecord(
                urlkey=urlkey,
                timestamp=datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
                    tzinfo=timezone.utc
                ),
                original=original,
                mimetype=mimetype,
                statuscode=None if status == "-" else int(status),
                digest=digest,
                length=None if length == "-" else int(length),
            )

    def close(self):
        self.closed = True


def patch_cdx(body: bytes):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_cdx
    rows = json.loads(body)

    def fake_fetch_cdx(**kwargs):
        previous = cdx_mod.WaybackClient
        cdx_mod.WaybackClient = lambda *args, **kw: FakeCdxClient(rows)
        try:
            return original(**kwargs)
        finally:
            cdx_mod.WaybackClient = previous

    cdx_mod.fetch_cdx = fake_fetch_cdx
    fetch_mod.fetch_cdx = fake_fetch_cdx
    return original, cdx_mod, fetch_mod


def patch_cdx_by_year(bodies_by_year: dict[int, bytes]):
    from archive_magic_fetch import cdx as cdx_mod
    from archive_magic_fetch import fetch as fetch_mod

    original = cdx_mod.fetch_cdx

    def fake_fetch_cdx(**kwargs):
        year = int(str(kwargs["date_start"])[:4])
        rows = json.loads(bodies_by_year.get(year, b"[]"))
        previous = cdx_mod.WaybackClient
        cdx_mod.WaybackClient = lambda *args, **kw: FakeCdxClient(rows)
        try:
            return original(**kwargs)
        finally:
            cdx_mod.WaybackClient = previous

    cdx_mod.fetch_cdx = fake_fetch_cdx
    fetch_mod.fetch_cdx = fake_fetch_cdx
    return original, cdx_mod, fetch_mod


def memento_client(
    identity,
    body: bytes,
    *,
    headers: dict | None = None,
    returned_url: str | None = None,
):
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
            memento.url = (
                returned_url
                if returned_url is not None
                else identity.original_url
            )
            return memento

    return Client()


def substitution_client(slash_url: str, found_ts: str):
    """Client whose exact playback is a Wayback slash-normalizing 302."""

    from wayback.exceptions import MementoPlaybackError

    location = f"https://web.archive.org/web/{found_ts}id_/{slash_url}"
    response = MagicMock()
    response.headers = {
        "X-Archive-Redirect-Reason": f"found capture at {found_ts}",
        "Location": location,
    }

    class Session:
        def request(self, method, url, **kwargs):
            return response

    class Client:
        def __init__(self):
            self.session = Session()
            self.calls = 0

        def get_memento(self, *args, **kwargs):
            self.calls += 1
            self.session.request(
                "GET", "https://web.archive.org/web/x", allow_redirects=False
            )
            raise MementoPlaybackError("could not be played")

    return Client()


def found_capture_client(
    nearby_url: str,
    found_ts: str,
    body: bytes,
    *,
    status: int = 200,
):
    """Client whose exact playback is a found-capture-at 302 to another URL."""

    from datetime import datetime, timezone

    from wayback.exceptions import MementoPlaybackError

    location = f"https://web.archive.org/web/{found_ts}id_/{nearby_url}"
    response = MagicMock()
    response.headers = {
        "X-Archive-Redirect-Reason": f"found capture at {found_ts}",
        "Location": location,
    }

    class Session:
        def request(self, method, url, **kwargs):
            return response

    class Client:
        def __init__(self):
            self.session = Session()
            self.calls = 0

        def get_memento(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                self.session.request(
                    "GET", "https://web.archive.org/web/x", allow_redirects=False
                )
                raise MementoPlaybackError("could not be played")
            memento = MagicMock()
            memento.__enter__ = lambda s: s
            memento.__exit__ = MagicMock(return_value=False)
            memento.content = body
            memento.status_code = status
            memento.memento_url = location
            memento.timestamp = datetime(
                int(found_ts[0:4]),
                int(found_ts[4:6]),
                int(found_ts[6:8]),
                int(found_ts[8:10]),
                int(found_ts[10:12]),
                int(found_ts[12:14]),
                tzinfo=timezone.utc,
            )
            memento.headers = {"Content-Type": "text/html"}
            memento.url = nearby_url
            return memento

    return Client()
