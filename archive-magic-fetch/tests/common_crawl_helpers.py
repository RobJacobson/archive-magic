"""Generated source bytes and fake HTTP responses; no public-service traffic."""

import gzip
import json
from io import BytesIO

import requests

from archive_magic_fetch.archive.identity import cdx_timestamp_to_warc_date, make_identity, payload_digest
from archive_magic_fetch.models import CaptureRef, CommonCrawlLocator


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = headers or {}
        self.raw = Raw(self.body)
        self.closed = False

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code), response=self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Raw(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.reads = []

    def read(self, size=-1, *, decode_content=False):
        assert not decode_content
        self.reads.append(size)
        return super().read(size)


class Client:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False
        self.headers = {}
        self.mounts = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.handler(url, **kwargs)

    def mount(self, prefix, adapter):
        self.mounts[prefix] = adapter

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def record(*, timestamp="20170615000000", body=b"hello", status="200", headers=(),
           warc_headers=(), kind="response", length_delta=0):
    http = (f"HTTP/1.1 {status} Fixture\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n").encode()
    block = http + body
    fields = [
        ("WARC-Type", kind), ("WARC-Record-ID", "<urn:uuid:00000000-0000-0000-0000-000000000001>"),
        ("WARC-Target-URI", "https://example.org/"),
        ("WARC-Date", cdx_timestamp_to_warc_date(timestamp)),
        ("Content-Type", "application/http; msgtype=response"),
        ("WARC-Payload-Digest", payload_digest(body)),
        ("WARC-Block-Digest", payload_digest(block)),
        ("Content-Length", str(len(block) + length_delta)),
    ]
    for name, value in warc_headers:
        fields = [(k, v) for k, v in fields if k.lower() != name.lower()]
        if value is not None:
            fields.append((name, value))
    raw = ("WARC/1.0\r\n" + "".join(f"{k}: {v}\r\n" for k, v in fields) + "\r\n").encode() + block + b"\r\n\r\n"
    data = gzip.compress(raw)
    capture = CaptureRef(
        make_identity(original_url="https://example.org/", timestamp=timestamp, status_token=status, payload_digest=payload_digest(body)),
        "text/html", CommonCrawlLocator("CC-MAIN-2017-26", f"crawl-data/CC-MAIN-2017-26/{timestamp}.warc.gz", 100, len(data)),
    )
    return capture, data


def row(capture):
    identity, locator = capture.identity, capture.locator
    return {"urlkey": identity.urlkey, "url": identity.original_url, "timestamp": identity.timestamp,
            "status": identity.status_token, "digest": identity.payload_digest, "mime": capture.mime,
            "filename": locator.filename, "offset": str(locator.offset), "length": str(locator.length)}


def collection(crawl_id="CC-MAIN-2017-26", start="2017-01-01T00:00:00", end="2017-12-31T23:59:59"):
    return {"id": crawl_id, "cdx-api": f"https://index.commoncrawl.org/{crawl_id}-index", "from": start, "to": end}


def range_response(data, capture):
    locator = capture.locator
    return Response(data, status=206, headers={"Content-Range": f"bytes {locator.offset}-{locator.offset + locator.length - 1}/99999999", "Content-Length": str(locator.length)})
