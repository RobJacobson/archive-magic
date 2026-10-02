from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

import pytest
from archive_magic_navigator import config as navigator_config
from archive_magic_navigator.collections import (
    Archive,
    ReplayCollection,
    select_archive_root,
)
from archive_magic_navigator.config import build_config, write_config
from archive_magic_navigator.errors import StartupError
from archive_magic_navigator.local import LocalArchiveStore
from archive_magic_navigator.process import find_wayback, run_wayback
from archive_magic_navigator.validation import validate_archive

FIXTURE = Path(__file__).parent / "fixtures" / "collection"


def archive_for_root(archive_id: str, root: Path) -> Archive:
    collection_root = root
    replay_index = collection_root / f"{archive_id}-2020-index.cdxj"
    return Archive(
        archive_id,
        root.resolve(),
        (ReplayCollection("2020", collection_root.resolve(), replay_index.resolve()),),
    )


def copy_fixture(archives: Path, archive_id: str) -> Archive:
    root = archives / archive_id
    shutil.copytree(FIXTURE, root)
    collection_root = root
    if archive_id != "fixture":
        old_index = collection_root / "fixture-2020-index.cdxj"
        text = old_index.read_text(encoding="utf-8").replace(
            "fixture-2020-", f"{archive_id}-2020-"
        )
        for path in sorted(collection_root.glob("fixture-2020-*.warc.gz")):
            path.rename(collection_root / path.name.replace("fixture", archive_id, 1))
        new_index = collection_root / f"{archive_id}-2020-index.cdxj"
        new_index.write_text(text, encoding="utf-8")
        old_index.unlink()
    return archive_for_root(archive_id, root)


def snapshot_tree(root: Path):
    result = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        relative = path.relative_to(root).as_posix()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        result[relative] = (
            stat.st_mode,
            stat.st_size,
            stat.st_mtime_ns,
            digest,
        )
    return result


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def pywb_server(
    tmp_path,
    collections,
    *,
    wayback_fallback=False,
    child_environment=None,
):
    runtime = tmp_path / f"runtime-{free_port()}"
    runtime.mkdir()
    write_config(
        runtime,
        build_config(
            collections,
            wayback_fallback=wayback_fallback,
        ),
    )
    port = free_port()
    log = (runtime / "pywb.log").open("wb")
    child = subprocess.Popen(
        [
            find_wayback(),
            "--directory",
            str(runtime),
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=child_environment,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                log.flush()
                pytest.fail((runtime / "pywb.log").read_text(errors="replace"))
            try:
                with urlopen(base + "/", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("pywb integration server did not become ready")
        yield base
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log.close()


def get(url):
    with urlopen(url, timeout=5) as response:
        return response.status, response.read(), response.headers


@contextmanager
def private_s3_server(root: Path, data_prefix: bool = False):
    class S3Handler(BaseHTTPRequestHandler):
        ranges: ClassVar[list[tuple[str, str | None, str | None]]] = []

        def do_GET(self):
            path = unquote(urlparse(self.path).path).removeprefix("/bucket/")
            if data_prefix:
                path = path.removeprefix("data/")
            try:
                source = (root / path).resolve()
                source.relative_to(root.resolve())
            except (OSError, ValueError):
                self.send_error(404)
                return
            if not source.is_file():
                self.send_error(404)
                return

            range_header = self.headers.get("Range")
            authorization = self.headers.get("Authorization")
            type(self).ranges.append((path, range_header, authorization))
            if not authorization or not authorization.startswith(
                "AWS4-HMAC-SHA256 "
            ):
                self.send_error(403, "AWS Signature Version 4 is required")
                return
            data = source.read_bytes()
            if not range_header or not range_header.startswith("bytes="):
                self.send_error(400, "a byte range is required")
                return
            start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
            start = int(start_text)
            end = min(int(end_text), len(data) - 1)
            body = data[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), S3Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, S3Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class SentinelHandler(BaseHTTPRequestHandler):
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"live response")

    def log_message(self, format, *args):
        pass


class MementoHandler(BaseHTTPRequestHandler):
    capture_timestamp = "20200101000000"
    capture_datetime = "Wed, 01 Jan 2020 00:00:00 GMT"
    timegate_requests = []
    resource_requests = []

    def do_HEAD(self):
        original = unquote(self.path.removeprefix("/web/"))
        type(self).timegate_requests.append(
            (original, self.headers.get("Accept-Datetime"))
        )
        memento = (
            f"http://127.0.0.1:{self.server.server_port}/web/"
            f"{self.capture_timestamp}id_/{original}"
        )
        links = (
            f'<{original}>; rel="original", '
            f'<{memento}>; rel="memento"; '
            f'datetime="{self.capture_datetime}"'
        )
        self.send_response(200)
        self.send_header("Link", links)
        self.end_headers()

    def do_GET(self):
        type(self).resource_requests.append(self.path)
        if self.path.endswith("/http://fallback.test/"):
            body = (
                b"<!doctype html><html><head>"
                b'<link rel="stylesheet" '
                b'href="http://fallback.test/asset.css">'
                b"</head><body>Wayback fallback page</body></html>"
            )
            content_type = "text/html; charset=utf-8"
        elif self.path.endswith("/http://fallback.test/asset.css"):
            body = b"body { background: rgb(1, 2, 3); }\n"
            content_type = "text/css"
        else:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Memento-Datetime", self.capture_datetime)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


@contextmanager
def memento_server():
    class IsolatedMementoHandler(MementoHandler):
        timegate_requests = []
        resource_requests = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), IsolatedMementoHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = f"memento+http://127.0.0.1:{server.server_port}/web/"
        yield source, IsolatedMementoHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.integration
def test_real_pywb_replays_versions_revisit_and_subresources_read_only(
    tmp_path,
):
    archives = tmp_path / "archives"
    collection = copy_fixture(archives, "fixture")
    assert validate_archive(collection).record_count == 7
    replay_index = collection.collections[0].replay_index
    replay_filenames = {
        json.loads(line.split(" ", 2)[2])["filename"]
        for line in replay_index.read_text().splitlines()
    }
    assert replay_filenames == {
        "fixture-2020-001.warc.gz",
        "fixture-2020-002.warc.gz",
        "fixture-2020-003.warc.gz",
    }
    before = snapshot_tree(archives)

    SentinelHandler.requests = 0
    try:
        sentinel = ThreadingHTTPServer(
            ("127.0.0.1", 18765),
            SentinelHandler,
        )
    except OSError as error:
        pytest.skip(f"sentinel port unavailable: {error}")
    thread = threading.Thread(target=sentinel.serve_forever, daemon=True)
    thread.start()
    try:
        with pywb_server(
            tmp_path,
            [collection],
        ) as base:
            status, home, _ = get(base + "/")
            assert status == 200
            assert b"Archive Magic Navigator" in home
            assert b"fixture" in home

            status, search, _ = get(base + "/fixture/")
            assert status == 200
            assert b"Find snapshots" in search
            assert b'window.location.assign("/fixture/*/" + value)' in search
            assert b"/fixture//fixture/" not in search

            _, cdx, _ = get(
                base + "/fixture/cdx?url=http%3A%2F%2Fexample.test%2F" + "&output=json"
            )
            records = [json.loads(line) for line in cdx.splitlines()]
            assert [record["timestamp"] for record in records] == [
                "20200101000000",
                "20210101000000",
                "20220101000000",
            ]
            assert records[-1]["mime"] == "warc/revisit"

            _, first, _ = get(
                base + "/fixture/20200101000000mp_/" + "http://example.test/"
            )
            _, second, _ = get(
                base + "/fixture/20210101000000id_/" + "http://example.test/"
            )
            _, revisit, _ = get(
                base + "/fixture/20220101000000id_/" + "http://example.test/"
            )
            assert b"Archived version one" in first
            assert (
                b"/fixture/20200101000000cs_/http://example.test/assets/site.css"
            ) in first
            assert b"Archived version two" in second
            assert revisit == second

            _, css, headers = get(
                base
                + "/fixture/20200101000001id_/"
                + "http://example.test/assets/site.css"
            )
            assert css == b"body { color: rgb(18, 52, 86); }\n"
            assert headers.get_content_type() == "text/css"

            _, local_redirect_target, _ = get(
                base + "/fixture/20200101000003mp_/" + "http://local-redirect.test/"
            )
            assert b"Redirect target captured locally" in local_redirect_target

            with pytest.raises(HTTPError) as raised:
                get(
                    base
                    + "/fixture/20200101000000im_/"
                    + "http://127.0.0.1:18765/live-only.png"
                )
            assert raised.value.code == 404
            assert SentinelHandler.requests == 0
    finally:
        sentinel.shutdown()
        sentinel.server_close()
        thread.join(timeout=2)

    assert snapshot_tree(archives) == before


@pytest.mark.integration
def test_real_pywb_reads_private_s3_warc_byte_ranges(tmp_path):
    collection_root = FIXTURE.resolve()
    replay = ReplayCollection(
        "2020",
        collection_root,
        collection_root / "fixture-2020-index.cdxj",
        "s3://bucket/",
    )
    archive = Archive("fixture", FIXTURE.resolve(), (replay,))

    with private_s3_server(FIXTURE.resolve()) as (server, handler):
        environment = os.environ.copy()
        environment.update(
            {
                "AWS_ACCESS_KEY_ID": "test-read-key",
                "AWS_SECRET_ACCESS_KEY": "test-read-secret",
                "AWS_ENDPOINT_URL_S3": (f"http://127.0.0.1:{server.server_port}"),
                "AWS_REGION": "us-east-1",
                "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_EC2_METADATA_DISABLED": "true",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        with pywb_server(
            tmp_path,
            [archive],
            child_environment=environment,
        ) as base:
            _, body, _ = get(
                base + "/fixture/20200101000000id_/" + "http://example.test/"
            )

    assert b"Archived version one" in body
    assert handler.ranges
    assert all(
        "/" not in filename
        and byte_range is not None
        and byte_range.startswith("bytes=")
        and authorization is not None
        and authorization.startswith("AWS4-HMAC-SHA256 ")
        for filename, byte_range, authorization in handler.ranges
    )


@pytest.mark.integration
def test_real_pywb_uses_wayback_fallback_for_redirect_and_assets(
    tmp_path,
    monkeypatch,
):
    archives = tmp_path / "archives"
    collection = copy_fixture(archives, "fixture")
    assert validate_archive(collection).record_count == 7
    before = snapshot_tree(archives)
    collection = LocalArchiveStore(
        collection.root, tmp_path / "cache", 300
    ).load_archive("fixture")

    with memento_server() as (source, handler):
        monkeypatch.setattr(
            navigator_config,
            "WAYBACK_MEMENTO_SOURCE",
            source,
        )
        with pywb_server(
            tmp_path,
            [collection],
            wayback_fallback=True,
        ) as base:
            _, local, _ = get(
                base + "/fixture/20200101000000id_/" + "http://example.test/"
            )
            assert b"Archived version one" in local
            assert handler.timegate_requests == []
            assert handler.resource_requests == []

            _, fallback, _ = get(
                base + "/fixture/20200101000002mp_/" + "http://redirect.test/"
            )
            assert b"Wayback fallback page" in fallback
            assert (
                b"/fixture/20200101000002cs_/http://fallback.test/asset.css"
            ) in fallback
            assert handler.timegate_requests == [
                (
                    "http://fallback.test/",
                    "Wed, 01 Jan 2020 00:00:02 GMT",
                )
            ]
            assert len(handler.resource_requests) == 1

            _, css, headers = get(
                base + "/fixture/20200101000002cs_/" + "http://fallback.test/asset.css"
            )
            assert css == b"body { background: rgb(1, 2, 3); }\n"
            assert headers.get_content_type() == "text/css"
            assert handler.timegate_requests[-1] == (
                "http://fallback.test/asset.css",
                "Wed, 01 Jan 2020 00:00:02 GMT",
            )
            assert len(handler.resource_requests) == 2

    assert snapshot_tree(archives) == before


@pytest.mark.integration
def test_real_pywb_lists_multiple_explicit_collections(tmp_path):
    archives = tmp_path / "archives"
    collections = []
    for collection_id in ("collection-a", "collection-b"):
        collection = copy_fixture(archives, collection_id)
        validate_archive(collection)
        collections.append(collection)
    before = snapshot_tree(archives)

    with pywb_server(
        tmp_path,
        collections,
    ) as base:
        _, home, _ = get(base + "/")
        assert b"collection-a" in home
        assert b"collection-b" in home
        _, body, _ = get(
            base + "/collection-a/20200101000000id_/" + "http://example.test/"
        )
        assert b"Archived version one" in body

    assert snapshot_tree(archives) == before


@pytest.mark.integration
def test_readiness_does_not_accept_an_unrelated_service(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fixture = FIXTURE.resolve()
    write_config(runtime, build_config([archive_for_root("fixture", fixture)]))
    ready = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), SentinelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(StartupError, match="port .* is already in use"):
            run_wayback(
                runtime,
                "127.0.0.1",
                server.server_port,
                debug=False,
                on_ready=ready.append,
                startup_timeout=5,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert ready == []


@pytest.mark.integration
def test_run_wayback_accepts_its_private_readiness_marker(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    fixture = FIXTURE.resolve()
    write_config(runtime, build_config([archive_for_root("fixture", fixture)]))
    ready = []

    def stop_after_ready(url):
        ready.append(url)
        raise KeyboardInterrupt

    port = free_port()
    assert (
        run_wayback(
            runtime,
            "127.0.0.1",
            port,
            debug=False,
            on_ready=stop_after_ready,
            startup_timeout=5,
        )
        == 0
    )
    assert ready == [f"http://127.0.0.1:{port}/"]


@pytest.mark.integration
def test_real_pywb_aggregates_flat_collections_and_replays_same_collection_revisit(
    tmp_path,
):
    """Same-year shard revisits and a later-year revisit of that URL must replay."""

    from io import BytesIO

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    archives = tmp_path / "archives"
    root = archives / "annual"
    year_dir = root
    year_dir.mkdir(parents=True)

    url = "http://example.org/"
    body = b"<!doctype html><html><body>Annual shard body</body></html>"
    headers = StatusAndHeaders(
        "200 OK",
        [("Content-Type", "text/html; charset=utf-8")],
        protocol="HTTP/1.1",
    )
    entries = []

    warc001 = year_dir / "annual-2020-001.warc.gz"
    with warc001.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.1")
        record = writer.create_warc_record(
            url,
            "response",
            payload=BytesIO(body),
            http_headers=headers,
            warc_headers_dict={"WARC-Date": "2020-06-01T00:00:00Z"},
        )
        start = stream.tell()
        writer.write_record(record)
        length = stream.tell() - start
        digest = record.rec_headers.get_header("WARC-Payload-Digest")
        entries.append(
            (
                "org,example)/",
                "20200601000000",
                {
                    "url": url,
                    "mime": "text/html",
                    "status": "200",
                    "digest": digest,
                    "filename": warc001.name,
                    "offset": str(start),
                    "length": str(length),
                },
            )
        )

    warc002 = year_dir / "annual-2020-002.warc.gz"
    with warc002.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.1")
        revisit = writer.create_revisit_record(
            url,
            digest,
            url,
            "2020-06-01T00:00:00Z",
            http_headers=headers,
            warc_headers_dict={"WARC-Date": "2020-07-01T00:00:00Z"},
        )
        start = stream.tell()
        writer.write_record(revisit)
        length = stream.tell() - start
        entries.append(
            (
                "org,example)/",
                "20200701000000",
                {
                    "url": url,
                    "mime": "warc/revisit",
                    "status": "200",
                    "digest": digest,
                    "filename": warc002.name,
                    "offset": str(start),
                    "length": str(length),
                },
            )
        )

    entries.sort(key=lambda item: (item[0], item[1]))
    index = year_dir / "annual-2020-index.cdxj"
    index.write_text(
        "".join(
            f"{key} {ts} {json.dumps(meta, separators=(',', ':'), sort_keys=True)}\n"
            for key, ts, meta in entries
        ),
        encoding="utf-8",
    )

    second_dir = root
    second_url = "http://second.example/"
    second_warc = second_dir / "annual-2021-001.warc.gz"
    second_entries = []
    with second_warc.open("wb") as stream:
        writer = WARCWriter(stream, gzip=True, warc_version="1.1")
        second = writer.create_warc_record(
            second_url,
            "response",
            payload=BytesIO(b"Second portable collection"),
            http_headers=headers,
            warc_headers_dict={"WARC-Date": "2021-01-01T00:00:00Z"},
        )
        start = stream.tell()
        writer.write_record(second)
        length = stream.tell() - start
        second_entries.append(
            (
                "example,second)/",
                "20210101000000",
                {
                    "url": second_url,
                    "mime": "text/html",
                    "status": "200",
                    "digest": second.rec_headers.get_header("WARC-Payload-Digest"),
                    "filename": second_warc.name,
                    "offset": str(start),
                    "length": str(length),
                },
            )
        )
        later = writer.create_revisit_record(
            url,
            digest,
            url,
            "2020-06-01T00:00:00Z",
            http_headers=headers,
            warc_headers_dict={"WARC-Date": "2021-06-01T00:00:00Z"},
        )
        start = stream.tell()
        writer.write_record(later)
        length = stream.tell() - start
        second_entries.append(
            (
                "org,example)/",
                "20210601000000",
                {
                    "url": url,
                    "mime": "warc/revisit",
                    "status": "200",
                    "digest": digest,
                    "filename": second_warc.name,
                    "offset": str(start),
                    "length": str(length),
                },
            )
        )
    second_entries.sort(key=lambda item: (item[0], item[1]))
    (second_dir / "annual-2021-index.cdxj").write_text(
        "".join(
            f"{key} {ts} {json.dumps(meta, separators=(',', ':'), sort_keys=True)}\n"
            for key, ts, meta in second_entries
        ),
        encoding="utf-8",
    )

    collection = select_archive_root(archives / "annual", "annual")
    assert validate_archive(collection).record_count == 4
    assert [item.collection_id for item in collection.collections] == ["2020", "2021"]
    before = snapshot_tree(archives)

    snapshot = LocalArchiveStore(
        collection.root, tmp_path / "cache", 300
    ).load_archive("annual")
    with pywb_server(tmp_path, [snapshot]) as base:
        _, original, _ = get(base + "/annual/20200601000000id_/http://example.org/")
        _, revisited, _ = get(base + "/annual/20200701000000id_/http://example.org/")
        assert b"Annual shard body" in original
        assert revisited == original
        _, later_body, _ = get(base + "/annual/20210601000000id_/http://example.org/")
        assert later_body == original
        _, second_body, _ = get(
            base + "/annual/20210101000000id_/http://second.example/"
        )
        assert b"Second portable collection" in second_body

    assert snapshot_tree(archives) == before


@pytest.mark.integration
@pytest.mark.parametrize("source_type", ["local", "remote"])
def test_running_pywb_adopts_updates_and_new_year_and_survives_failure(
    tmp_path, monkeypatch, source_type,
):
    from contextlib import ExitStack
    from io import BytesIO

    from archive_magic_navigator.errors import ValidationError
    from archive_magic_navigator.remote import RemoteArchiveStore
    from archive_magic_navigator.settings import RemoteSource
    from test_settings_remote import FakeRemoteS3
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    source = copy_fixture(tmp_path / "archives", "fixture").root
    index = source / "fixture-2020-index.cdxj"
    full_index = index.read_bytes()
    index.write_bytes(b"".join(
        line for line in full_index.splitlines(keepends=True)
        if b"20210101000000" not in line
    ))
    cache = tmp_path / "cache"
    with ExitStack() as stack:
        environment = None
        fake = FakeRemoteS3()

        def upload():
            for path in source.iterdir():
                if path.suffix in {".cdxj", ".gz"}:
                    fake.seed("data/" + path.name, path.read_bytes())

        if source_type == "local":
            store = LocalArchiveStore(source, cache, 0.05)
        else:
            server, handler = stack.enter_context(private_s3_server(source, data_prefix=True))
            upload()
            monkeypatch.setattr("archive_magic_navigator.remote.boto3.client", lambda *a, **k: fake)
            store = RemoteArchiveStore(
                RemoteSource("bucket", "", f"http://127.0.0.1:{server.server_port}", "us-east-1"),
                cache, 0.05,
            )
            environment = store.child_environment()
            environment.update({
                "AWS_ACCESS_KEY_ID": "test-read-key",
                "AWS_SECRET_ACCESS_KEY": "test-read-secret",
                "AWS_EC2_METADATA_DISABLED": "true",
                "NO_PROXY": "127.0.0.1,localhost",
            })
        archive = store.load_archive("fixture")
        base = stack.enter_context(pywb_server(tmp_path, [archive], child_environment=environment))
        replay = base + "/fixture/"
        assert b"Archived version one" in get(replay + "20200101000000id_/http://example.test/")[1]
        assert b"20210101000000" not in get(replay + "cdx?url=http://example.test/&output=json")[1]

        # Change an existing annual index while this exact server remains alive.
        index.write_bytes(full_index)
        upload()
        store._poll_archive("fixture")
        assert b"Archived version two" in get(replay + "20210101000000id_/http://example.test/")[1]

        # Publish a WARC, then its index for a year absent at startup.
        warc = source / "fixture-2021-001.warc.gz"
        with warc.open("wb") as stream:
            writer = WARCWriter(stream, gzip=True)
            record = writer.create_warc_record(
                "http://new-year.test/", "response", payload=BytesIO(b"New annual capture"),
                http_headers=StatusAndHeaders("200 OK", [("Content-Type", "text/html")], protocol="HTTP/1.1"),
                warc_headers_dict={"WARC-Date": "2021-01-01T00:00:00Z"},
            )
            writer.write_record(record)
        new_index = source / "fixture-2021-index.cdxj"
        new_index.write_text('test,new-year)/ 20210101000000 ' + json.dumps({
            "url": "http://new-year.test/", "mime": "text/html", "status": "200",
            "filename": warc.name, "offset": "0", "length": str(warc.stat().st_size),
        }) + '\n')
        upload()
        # Exercise the real polling lifecycle, including the configured interval.
        store.start_polling()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if b"new-year.test" in archive.replay_index.read_bytes():
                    break
                time.sleep(0.02)
            else:
                pytest.fail("poller did not adopt new year")
        finally:
            store.stop_polling()
        assert b"New annual capture" in get(replay + "20210101000000id_/http://new-year.test/")[1]

        before = archive.replay_index.read_bytes()
        index.write_bytes(b"incomplete publication")
        upload()
        with pytest.raises(ValidationError):
            store._poll_archive("fixture")
        assert archive.replay_index.read_bytes() == before
        assert b"Archived version one" in get(replay + "20200101000000id_/http://example.test/")[1]
        assert b"New annual capture" in get(replay + "20210101000000id_/http://new-year.test/")[1]
        if source_type == "remote":
            assert handler.ranges
            assert all(byte_range and auth for _, byte_range, auth in handler.ranges)
            assert all(key == "list" or key.endswith(".cdxj") for key, _ in fake.calls)
        assert not list(cache.rglob("*.warc.gz"))
