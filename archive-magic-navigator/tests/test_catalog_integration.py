"""Real Navigator + pywb against an authenticated, mutable multi-bucket S3 fixture."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import pytest
from test_pywb_integration import FIXTURE, free_port, get

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=')


class BucketFixture:
    def __init__(self):
        self.objects = {}
        self.requests = []
        self.offline = set()

    def seed(self, bucket, archive_id, prefix=''):
        root = prefix + '/' if prefix else ''
        manifest = {'id': archive_id, 'name': 'Organization ' + archive_id,
                    'homepage': 'http://example.test/', 'description': 'Preserved history from an independent private bucket.',
                    'logo': {'src': 'assets/logo.png', 'alt': 'Logo ' + archive_id},
                    'preview': {'src': 'assets/preview.png', 'alt': 'Website preview ' + archive_id}}
        self.objects[bucket, root + 'archive.json'] = json.dumps(manifest).encode()
        self.objects[bucket, root + 'assets/logo.png'] = PNG
        self.objects[bucket, root + 'assets/preview.png'] = PNG
        for path in FIXTURE.iterdir():
            if path.name.endswith(('.cdxj', '.warc.gz')):
                name = path.name.replace('fixture-', archive_id + '-', 1)
                data = path.read_bytes()
                if path.name.endswith('.cdxj'):
                    data = data.replace(b'fixture-', (archive_id + '-').encode())
                self.objects[bucket, root + 'data/' + name] = data


@contextmanager
def bucket_server(fixture):
    class Handler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parsed = urlsplit(self.path)
            bucket, _, key = unquote(parsed.path).lstrip('/').partition('/')
            args = parse_qs(parsed.query)
            auth = self.headers.get('Authorization', '')
            fixture.requests.append((bucket, key, dict(self.headers)))
            if not auth.startswith('AWS4-HMAC-SHA256 '):
                self.send_error(403)
                return
            if bucket in fixture.offline:
                self.send_error(503)
                return
            if 'list-type' in args:
                prefix = args.get('prefix', [''])[0]
                delimiter = args.get('delimiter', [''])[0]
                items = []
                for (b, k), data in list(fixture.objects.items()):
                    if b != bucket or not k.startswith(prefix) or (delimiter and delimiter in k[len(prefix):]):
                        continue
                    etag = hashlib.md5(data).hexdigest()
                    items.append(f'<Contents><Key>{escape(k)}</Key><Size>{len(data)}</Size><ETag>"{etag}"</ETag></Contents>')
                body = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>' + ''.join(items) + '</ListBucketResult>').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
            else:
                data = fixture.objects.get((bucket, key))
                if data is None:
                    self.send_error(404)
                    return
                etag = '"' + hashlib.md5(data).hexdigest() + '"'
                if self.headers.get('If-None-Match') == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                if self.headers.get('If-Match', etag) != etag:
                    self.send_error(412)
                    return
                body = data
                byte_range = self.headers.get('Range')
                if byte_range:
                    start, end = byte_range.removeprefix('bytes=').split('-')
                    start, end = int(start), min(int(end) if end else len(data)-1, len(data)-1)
                    body = data[start:end+1]
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                else:
                    self.send_response(200)
                self.send_header('ETag', etag)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@contextmanager
def navigator_server(root, endpoint, entries, *, port=None, poll=0.2):
    root.mkdir(parents=True, exist_ok=True)
    catalog = root/'catalog.json'
    catalog.write_text(json.dumps({'title': 'Website Archive', 'storage': {'endpoint_url': endpoint, 'region': 'us-east-1'}, 'archives': entries}))
    port = port or free_port()
    environment = {**os.environ, 'AWS_ACCESS_KEY_ID': 'fixture', 'AWS_SECRET_ACCESS_KEY': 'fixture-secret', 'AWS_EC2_METADATA_DISABLED': 'true', 'NO_PROXY': 'localhost,127.0.0.1'}
    with (root/'server.log').open('wb') as log:
        process = subprocess.Popen([sys.executable, '-c', 'from archive_magic_navigator.cli import main; raise SystemExit(main())', '--catalog', str(catalog), '--port', str(port), '--poll-interval', str(poll), '--wayback-fallback', 'off'], stdout=log, stderr=log, env=environment)
        base = f'http://127.0.0.1:{port}'
        try:
            await_page(base, b'Website Archive', process, root)
            yield base, process
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def await_page(base, expected, process, root):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail((root/'server.log').read_text())
        try:
            html = get(base + '/')[1]
            if expected in html:
                return html
        except OSError:
            pass
        time.sleep(0.05)
    pytest.fail('Navigator did not serve expected page: ' + (root/'server.log').read_text())


@pytest.mark.integration
def test_catalog_private_buckets_cold_start_refresh_and_recovery(tmp_path):
    fixture = BucketFixture()
    fixture.seed('one', 'first')
    fixture.seed('two', 'second', 'archive')
    entries = [{'bucket':'one'}, {'bucket':'two', 'prefix':'archive'}, {'bucket':'three'}]
    root = tmp_path/'navigator'
    with bucket_server(fixture) as s3:
        with navigator_server(root, f'http://127.0.0.1:{s3.server_port}', entries) as (base, process):
            html = get(base+'/')[1]
            assert b'Organization first' in html and b'Organization second' in html
            assert b'Temporarily unavailable' in html
            assert b'Captures from 2020-01-01 to 2022-01-01' in html
            import re
            logo = re.search(rb'class="am-logo" src="([^"]+)"', html).group(1).decode()
            assert get(base + logo)[1] == PNG
            for archive_id in ('first', 'second'):
                assert b'Archived version one' in get(base + f'/{archive_id}/20200101000000id_/http://example.test/')[1]
            fixture.seed('three', 'third')
            await_page(base, b'Organization third', process, root)
            assert b'Archived version one' in get(base + '/third/20200101000000id_/http://example.test/')[1]
            manifest = json.loads(fixture.objects['one','archive.json'])
            manifest['name'] = '<Updated organization>'
            fixture.objects['one','archive.json'] = json.dumps(manifest).encode()
            html = await_page(base, b'&lt;Updated organization&gt;', process, root)
            assert b'<Updated organization>' not in html
            fixture.objects['one','archive.json'] = b'broken'
            await_page(base, b'Using cached information', process, root)
            assert b'Archived version one' in get(base + '/first/20200101000000id_/http://example.test/')[1]
            warc_requests = [(b,k,h) for b,k,h in fixture.requests if k.endswith('.warc.gz')]
            assert {b for b,_,_ in warc_requests} == {'one','two','three'}
            assert all(h.get('Range') and h.get('Authorization') for _,_,h in warc_requests)
            assert not list(root.rglob('*.warc.gz'))
            assert not list(root.rglob('fetch.toml'))


@pytest.mark.integration
def test_all_unavailable_catalog_starts_and_recovers(tmp_path):
    fixture = BucketFixture()
    root = tmp_path/'navigator'
    with bucket_server(fixture) as s3:
        with navigator_server(root, f'http://127.0.0.1:{s3.server_port}', [{'bucket':'one'}]) as (base, process):
            assert b'Temporarily unavailable' in get(base+'/')[1]
            fixture.seed('one', 'first')
            await_page(base, b'Organization first', process, root)
            assert b'Archived version one' in get(base + '/first/20200101000000id_/http://example.test/')[1]


@pytest.mark.integration
def test_bucket_replay_after_local_eviction_and_cache_clear(tmp_path):
    import shutil
    fixture = BucketFixture()
    fixture.seed('one', 'first')
    fixture.objects['one', 'discovery/wayback/v1/opaque/2020.cdx.json'] = b'not a Navigator input'
    root = tmp_path / 'workspace'
    local = root / 'archives' / 'first' / 'data'
    local.mkdir(parents=True)
    (local / 'local-only.warc.gz').write_bytes(b'local copy')
    entries = [{'bucket': 'one'}]
    with bucket_server(fixture) as s3:
        endpoint = f'http://127.0.0.1:{s3.server_port}'
        with navigator_server(root, endpoint, entries) as (base, process):
            shutil.rmtree(root / 'archives')
            assert b'Archived version one' in get(base + '/first/20200101000000id_/http://example.test/')[1]
        assert (root / 'cache' / 'navigator').is_dir()
        shutil.rmtree(root / 'cache')
        with navigator_server(root, endpoint, entries) as (base, process):
            assert b'Archived version one' in get(base + '/first/20200101000000id_/http://example.test/')[1]
        assert not (root / 'archives').exists()
        assert not any('/discovery/' in key or key.startswith('discovery/') for _, key, _ in fixture.requests)
        assert not list(root.rglob('*.warc.gz'))
