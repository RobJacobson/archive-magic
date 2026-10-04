"""Shared fixtures and helpers for Archive Magic Fetch tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

from archive_magic_fetch.archive.identity import make_identity, payload_digest
from archive_magic_fetch.archive.layout import ArchiveLayout, ensure_collection_dirs
from archive_magic_fetch.models import CaptureIdentity, CaptureResult
from archive_magic_fetch.pipeline.indexing import publish_collection_index
from archive_magic_fetch.pipeline.writing import _CollectionWarcWriter
from wayback import CdxRecord


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
) -> CaptureResult:
    return CaptureResult(
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
        from_date = kwargs.get("from_date")
        to_date = kwargs.get("to_date")
        for row in self.rows:
            if not isinstance(row, list) or len(row) < 7:
                continue
            urlkey, timestamp, original, mimetype, status, digest, length = row[:7]
            if from_date is not None and str(timestamp) < str(from_date):
                continue
            if to_date is not None and str(timestamp) > str(to_date):
                continue
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
    import archive_magic_fetch.pipeline.discovery.wayback as cdx_mod

    original = cdx_mod._fetch_cdx
    rows = json.loads(body)

    def fake_fetch_cdx(**kwargs):
        previous = cdx_mod.WaybackClient
        cdx_mod.WaybackClient = lambda *args, **kw: FakeCdxClient(rows)
        try:
            return original(**kwargs)
        finally:
            cdx_mod.WaybackClient = previous

    cdx_mod._fetch_cdx = fake_fetch_cdx

    return original, cdx_mod


def patch_cdx_by_year(bodies_by_year: dict[int, bytes]):
    import archive_magic_fetch.pipeline.discovery.wayback as cdx_mod

    original = cdx_mod._fetch_cdx

    def fake_fetch_cdx(**kwargs):
        year = int(str(kwargs["date_start"])[:4])
        rows = json.loads(bodies_by_year.get(year, b"[]"))
        previous = cdx_mod.WaybackClient
        cdx_mod.WaybackClient = lambda *args, **kw: FakeCdxClient(rows)
        try:
            return original(**kwargs)
        finally:
            cdx_mod.WaybackClient = previous

    cdx_mod._fetch_cdx = fake_fetch_cdx

    return original, cdx_mod


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
                returned_url if returned_url is not None else identity.original_url
            )
            return memento

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


def fetch_memento(client, identity):
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.decoding.wayback import decode_memento
    from archive_magic_fetch.pipeline.retrieval.wayback import retrieve_memento

    capture = CaptureRef(identity, "text/html")
    return decode_memento(retrieve_memento(client, capture), capture)


def make_source(
    settings=None, *, client_factory=None, download=None, sleep=lambda _: None
):
    """Script acquisition while exercising the actual Wayback discovery and policy."""
    from contextlib import ExitStack, contextmanager
    from dataclasses import replace
    from pathlib import Path

    from archive_magic_fetch.adapters.wayback import build_source
    from archive_magic_fetch.adapters.wayback_session import ArchiveMagicWaybackSession

    source = build_source(
        index_directory=settings.index_directory
        if settings
        else Path("/unused-test-cache"),
        cdx_window_days=settings.cdx_window_days if settings else 28,
        cdx_page_limit=settings.cdx_page_limit if settings else 5000,
        sleep=sleep,
    )
    if client_factory is not None:

        @contextmanager
        def open_client(stats):
            owner = client_factory()
            with ExitStack() as stack:
                if hasattr(owner, "__enter__"):
                    client = stack.enter_context(owner)
                else:
                    client = owner
                    if hasattr(owner, "close"):
                        stack.callback(owner.close)
                if isinstance(
                    getattr(client, "session", None), ArchiveMagicWaybackSession
                ):
                    client.session.track_playback(stats)
                yield client

        source = replace(source, open_client=open_client)
    if download is not None:
        source = replace(
            source, fetch=lambda client, capture: download(client, capture.identity)
        )
    return source


class AcquisitionHarness:
    """Compose production execution primitives for focused acquisition tests."""

    def __init__(
        self,
        source,
        *,
        sleep,
        pace=True,
        max_workers=4,
        starts_per_second=16,
        retries=4,
        trace_path=None,
        clock=None,
        report=None,
    ):
        import time

        from archive_magic_fetch.runtime.http import RequestStats
        from archive_magic_fetch.runtime.pacing import StartGate
        from archive_magic_fetch.runtime.reporting import emit
        from archive_magic_fetch.runtime.workers import CaptureWorkers

        self.source = source
        self.clock = clock or time.monotonic
        self.sleep = sleep
        self.max_attempts = retries + 1
        self._gate = StartGate(
            starts_per_second if pace else 0,
            clock=self.clock,
            sleep=sleep,
            report=report or emit,
        )
        self.request_stats = RequestStats(
            self._gate.wait,
            clock=self.clock,
            trace_path=trace_path,
            report=report or emit,
        )
        self.workers = CaptureWorkers(
            source, self.request_stats, max_workers=max_workers
        )
        self.max_workers = max_workers

    def download(self, identity):
        from archive_magic_fetch.models import CaptureRef
        from archive_magic_fetch.pipeline.retrieval.stage import fetch_capture

        return fetch_capture(
            CaptureRef(identity, "text/html"),
            source=self.source,
            client=self.workers.client,
            gate=self._gate,
            stats=self.request_stats,
            max_attempts=self.max_attempts,
            clock=self.clock,
            sleep=self.sleep,
        )

    def map(self, process, groups):
        return self.workers.map(process, groups)

    def submit(self, process, group):
        return self.workers._executor.submit(process, group)

    def close(self):
        try:
            self.workers.close()
        finally:
            self.request_stats.close()


def make_collection(root: Path) -> ArchiveLayout:
    layout = ArchiveLayout(root, "example.org")
    ensure_collection_dirs(layout)
    writer = _CollectionWarcWriter(layout, "2004")
    writer.write_playback(playback(make_capt()))
    writer.close()
    publish_collection_index(layout, "2004")
    return layout
