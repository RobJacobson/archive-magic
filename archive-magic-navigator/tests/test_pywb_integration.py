from __future__ import annotations

import hashlib
import json
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote
from urllib.request import urlopen

import pytest

from archive_magic_navigator import config as navigator_config
from archive_magic_navigator.collections import Archive, ReplayCollection, select_archive
from archive_magic_navigator.config import build_config, write_config
from archive_magic_navigator.errors import StartupError
from archive_magic_navigator.process import find_wayback, run_wayback
from archive_magic_navigator.validation import validate_archive


FIXTURE = Path(__file__).parent / "fixtures" / "collection"


def archive_for_root(archive_id: str, root: Path) -> Archive:
    collection_root = root / "collections" / "2020"
    replay_index = collection_root / f"{archive_id}-2020-index.cdxj"
    return Archive(
        archive_id,
        root.resolve(),
        (ReplayCollection("2020", collection_root.resolve(), replay_index.resolve()),),
    )


def copy_fixture(archives: Path, archive_id: str) -> Archive:
    root = archives / archive_id
    shutil.copytree(FIXTURE, root)
    collection_root = root / "collections" / "2020"
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
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else None
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
def pywb_server(tmp_path, collections, *, wayback_fallback=False):
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
        source = (
            f"memento+http://127.0.0.1:{server.server_port}/web/"
        )
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
                base
                + "/fixture/cdx?url=http%3A%2F%2Fexample.test%2F"
                + "&output=json"
            )
            records = [json.loads(line) for line in cdx.splitlines()]
            assert [record["timestamp"] for record in records] == [
                "20200101000000",
                "20210101000000",
                "20220101000000",
            ]
            assert records[-1]["mime"] == "warc/revisit"

            _, first, _ = get(
                base
                + "/fixture/20200101000000mp_/"
                + "http://example.test/"
            )
            _, second, _ = get(
                base
                + "/fixture/20210101000000id_/"
                + "http://example.test/"
            )
            _, revisit, _ = get(
                base
                + "/fixture/20220101000000id_/"
                + "http://example.test/"
            )
            assert b"Archived version one" in first
            assert (
                b"/fixture/20200101000000cs_/"
                b"http://example.test/assets/site.css"
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
                base
                + "/fixture/20200101000003mp_/"
                + "http://local-redirect.test/"
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
def test_real_pywb_uses_wayback_fallback_for_redirect_and_assets(
    tmp_path,
    monkeypatch,
):
    archives = tmp_path / "archives"
    collection = copy_fixture(archives, "fixture")
    assert validate_archive(collection).record_count == 7
    before = snapshot_tree(archives)

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
                base
                + "/fixture/20200101000000id_/"
                + "http://example.test/"
            )
            assert b"Archived version one" in local
            assert handler.timegate_requests == []
            assert handler.resource_requests == []

            _, fallback, _ = get(
                base
                + "/fixture/20200101000002mp_/"
                + "http://redirect.test/"
            )
            assert b"Wayback fallback page" in fallback
            assert (
                b"/fixture/20200101000002cs_/"
                b"http://fallback.test/asset.css"
            ) in fallback
            assert handler.timegate_requests == [
                (
                    "http://fallback.test/",
                    "Wed, 01 Jan 2020 00:00:02 GMT",
                )
            ]
            assert len(handler.resource_requests) == 1

            _, css, headers = get(
                base
                + "/fixture/20200101000002cs_/"
                + "http://fallback.test/asset.css"
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
            base
            + "/collection-a/20200101000000id_/"
            + "http://example.test/"
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
    """Full response in 001 and revisit in 002 of the same year must replay."""

    from io import BytesIO

    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    archives = tmp_path / "archives"
    root = archives / "annual"
    year_dir = root / "collections" / "2020"
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

    second_dir = root / "collections" / "2021"
    second_dir.mkdir()
    second_url = "http://second.example/"
    second_warc = second_dir / "annual-2021-001.warc.gz"
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
    (second_dir / "annual-2021-index.cdxj").write_text(
        "example,second)/ 20210101000000 "
        + json.dumps(
            {
                "url": second_url,
                "mime": "text/html",
                "status": "200",
                "digest": second.rec_headers.get_header("WARC-Payload-Digest"),
                "filename": second_warc.name,
                "offset": str(start),
                "length": str(length),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    collection = select_archive(archives.resolve(), "annual")
    assert validate_archive(collection).record_count == 3
    assert [item.collection_id for item in collection.collections] == ["2020", "2021"]
    before = snapshot_tree(archives)

    with pywb_server(tmp_path, [collection]) as base:
        _, original, _ = get(
            base + "/annual/20200601000000id_/http://example.org/"
        )
        _, revisited, _ = get(
            base + "/annual/20200701000000id_/http://example.org/"
        )
        assert b"Annual shard body" in original
        assert revisited == original
        _, second_body, _ = get(
            base + "/annual/20210101000000id_/http://second.example/"
        )
        assert b"Second portable collection" in second_body

    assert snapshot_tree(archives) == before
