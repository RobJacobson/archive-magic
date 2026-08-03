"""Regenerate the deterministic Navigator integration collection."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


ROOT = Path(__file__).parent / "collection"
WARC = ROOT / "archive" / "example.test" / "index.warc.gz"
LOCAL_REDIRECT_WARC = (
    ROOT / "archive" / "local-redirect.test" / "index.warc.gz"
)
LOCAL_TARGET_WARC = (
    ROOT / "archive" / "local-target.test" / "index.warc.gz"
)
INDEX = ROOT / "replay" / "index.cdxj"
HASHES = ROOT / "SHA256SUMS"
MAIN_URL = "http://example.test/"
CSS_URL = "http://example.test/assets/site.css"
REDIRECT_URL = "http://redirect.test/"
FALLBACK_URL = "http://fallback.test/"
LOCAL_REDIRECT_URL = "http://local-redirect.test/"
LOCAL_TARGET_URL = "http://local-target.test/"


def http_headers(
    content_type: str,
    *,
    status: str = "200 OK",
    extra: tuple[tuple[str, str], ...] = (),
) -> StatusAndHeaders:
    return StatusAndHeaders(
        status,
        [("Content-Type", content_type), *extra],
        protocol="HTTP/1.1",
    )


def main() -> None:
    for warc in (WARC, LOCAL_REDIRECT_WARC, LOCAL_TARGET_WARC):
        warc.parent.mkdir(parents=True, exist_ok=True)
    INDEX.parent.mkdir(parents=True, exist_ok=True)

    entries = []
    with WARC.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")

        first_body = (
            b"<!doctype html><html><head>"
            b'<link rel="stylesheet" href="http://example.test/assets/site.css">'
            b"</head><body><h1>Archived version one</h1>"
            b'<img src="http://127.0.0.1:18765/live-only.png">'
            b"</body></html>"
        )
        first = writer.create_warc_record(
            MAIN_URL,
            "response",
            payload=BytesIO(first_body),
            http_headers=http_headers("text/html; charset=utf-8"),
            warc_headers_dict={
                "WARC-Date": "2020-01-01T00:00:00Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000001>",
            },
        )
        start = stream.tell()
        writer.write_record(first)
        entries.append(
            entry(
                "test,example)/",
                "20200101000000",
                MAIN_URL,
                "text/html",
                first,
                start,
                stream.tell() - start,
            )
        )

        redirect = writer.create_warc_record(
            REDIRECT_URL,
            "response",
            payload=BytesIO(b""),
            http_headers=http_headers(
                "text/html; charset=utf-8",
                status="301 Moved Permanently",
                extra=(("Location", FALLBACK_URL),),
            ),
            warc_headers_dict={
                "WARC-Date": "2020-01-01T00:00:02Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000005>",
            },
        )
        start = stream.tell()
        writer.write_record(redirect)
        entries.append(
            entry(
                "test,redirect)/",
                "20200101000002",
                REDIRECT_URL,
                "text/html",
                redirect,
                start,
                stream.tell() - start,
                status="301",
            )
        )

        css = writer.create_warc_record(
            CSS_URL,
            "response",
            payload=BytesIO(b"body { color: rgb(18, 52, 86); }\n"),
            http_headers=http_headers("text/css"),
            warc_headers_dict={
                "WARC-Date": "2020-01-01T00:00:01Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000002>",
            },
        )
        start = stream.tell()
        writer.write_record(css)
        entries.append(
            entry(
                "test,example)/assets/site.css",
                "20200101000001",
                CSS_URL,
                "text/css",
                css,
                start,
                stream.tell() - start,
            )
        )

        second_body = (
            b"<!doctype html><html><head>"
            b'<link rel="stylesheet" href="http://example.test/assets/site.css">'
            b"</head><body><h1>Archived version two</h1></body></html>"
        )
        second = writer.create_warc_record(
            MAIN_URL,
            "response",
            payload=BytesIO(second_body),
            http_headers=http_headers("text/html; charset=utf-8"),
            warc_headers_dict={
                "WARC-Date": "2021-01-01T00:00:00Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000003>",
            },
        )
        start = stream.tell()
        writer.write_record(second)
        entries.append(
            entry(
                "test,example)/",
                "20210101000000",
                MAIN_URL,
                "text/html",
                second,
                start,
                stream.tell() - start,
            )
        )

        revisit = writer.create_revisit_record(
            MAIN_URL,
            second.rec_headers.get_header("WARC-Payload-Digest"),
            MAIN_URL,
            "2021-01-01T00:00:00Z",
            http_headers=http_headers("text/html; charset=utf-8"),
            warc_headers_dict={
                "WARC-Date": "2022-01-01T00:00:00Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000004>",
            },
        )
        start = stream.tell()
        writer.write_record(revisit)
        entries.append(
            entry(
                "test,example)/",
                "20220101000000",
                MAIN_URL,
                "warc/revisit",
                revisit,
                start,
                stream.tell() - start,
            )
        )

    with LOCAL_REDIRECT_WARC.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        local_redirect = writer.create_warc_record(
            LOCAL_REDIRECT_URL,
            "response",
            payload=BytesIO(b""),
            http_headers=http_headers(
                "text/html; charset=utf-8",
                status="301 Moved Permanently",
                extra=(("Location", LOCAL_TARGET_URL),),
            ),
            warc_headers_dict={
                "WARC-Date": "2020-01-01T00:00:03Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000006>",
            },
        )
        start = stream.tell()
        writer.write_record(local_redirect)
        entries.append(
            entry(
                "test,local-redirect)/",
                "20200101000003",
                LOCAL_REDIRECT_URL,
                "text/html",
                local_redirect,
                start,
                stream.tell() - start,
                status="301",
                filename="archive/local-redirect.test/index.warc.gz",
            )
        )

    with LOCAL_TARGET_WARC.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.0")
        local_target = writer.create_warc_record(
            LOCAL_TARGET_URL,
            "response",
            payload=BytesIO(b"<html>Redirect target captured locally</html>"),
            http_headers=http_headers("text/html; charset=utf-8"),
            warc_headers_dict={
                "WARC-Date": "2020-01-01T00:00:00Z",
                "WARC-Record-ID": "<urn:uuid:00000000-0000-0000-0000-000000000007>",
            },
        )
        start = stream.tell()
        writer.write_record(local_target)
        entries.append(
            entry(
                "test,local-target)/",
                "20200101000000",
                LOCAL_TARGET_URL,
                "text/html",
                local_target,
                start,
                stream.tell() - start,
                filename="archive/local-target.test/index.warc.gz",
            )
        )

    entries.sort(key=lambda item: (item[0], item[1]))
    INDEX.write_text(
        "".join(
            f"{url_key} {timestamp} "
            f"{json.dumps(payload, separators=(',', ':'), sort_keys=True)}\n"
            for url_key, timestamp, payload in entries
        ),
        encoding="utf-8",
    )
    hashes = []
    for path in (WARC, LOCAL_REDIRECT_WARC, LOCAL_TARGET_WARC, INDEX):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(f"{digest}  {path.relative_to(ROOT).as_posix()}\n")
    HASHES.write_text("".join(hashes), encoding="ascii")


def entry(
    url_key: str,
    timestamp: str,
    url: str,
    mime: str,
    record,
    offset: int,
    length: int,
    *,
    status: str = "200",
    filename: str = "archive/example.test/index.warc.gz",
):
    return (
        url_key,
        timestamp,
        {
            "digest": record.rec_headers.get_header("WARC-Payload-Digest"),
            "filename": filename,
            "length": str(length),
            "mime": mime,
            "offset": str(offset),
            "status": status,
            "url": url,
        },
    )


if __name__ == "__main__":
    main()
