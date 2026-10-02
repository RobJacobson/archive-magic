"""Bucket-owned presentation metadata and independently recoverable archives."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from pywb.utils.canonicalize import canonicalize

from .collections import Archive
from .config import build_config
from .errors import ValidationError
from .remote import LegacyLayoutError, RemoteArchiveStore
from .settings import CatalogConfig, RemoteSource, parse_manifest, web_url


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.tmp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def image_extension(data: bytes) -> str:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    raise ValueError('image must be PNG, JPEG, WebP, or GIF')


@dataclass
class Entry:
    source: RemoteSource
    store: RemoteArchiveStore | None = None
    metadata: dict | None = None
    archive: Archive | None = None
    images: dict = field(default_factory=dict)
    status: str = 'unavailable'
    error: str = ''
    summary_stamp: tuple | None = None
    summary: dict = field(default_factory=dict)


class CatalogStore:
    def __init__(self, config: CatalogConfig, cache: Path, runtime: Path, poll_seconds: float, fallback: bool):
        self.config = config
        self.cache = cache
        self.runtime = runtime
        self.poll_seconds = poll_seconds
        self.fallback = fallback
        self.entries = [Entry(source) for source in config.archives]
        self.claims: dict[str, Entry] = {}
        self._stop = threading.Event()
        self._thread = None

    def _cache_path(self, entry: Entry, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.cache / entry.source.cache_key / 'objects' / (digest + '.json')

    def _cached(self, entry: Entry, key: str) -> dict | None:
        try:
            value = json.loads(self._cache_path(entry, key).read_text())
            base64.b64decode(value['body'], validate=True)
            if not isinstance(value['etag'], str):
                return None
            return value
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _object(self, entry: Entry, key: str, limit: int) -> dict:
        cached = self._cached(entry, key)
        kwargs = {'Bucket': entry.source.bucket, 'Key': entry.source.key(key)}
        if cached:
            # R2 can return checksum headers on a 304 GET, which botocore
            # misparses as a streaming error body. HEAD avoids that path.
            current = entry.store.client.head_object(**kwargs)
            if current['ETag'] == cached['etag']:
                return cached
            kwargs['IfMatch'] = current['ETag']
        response = entry.store.client.get_object(**kwargs)
        try:
            data = response['Body'].read(limit + 1)
            if len(data) > limit:
                raise ValueError(f'{key} exceeds {limit} bytes')
            if 'ContentLength' in response and len(data) != response['ContentLength']:
                raise ValueError(f'{key}: incomplete download')
            return {'etag': response['ETag'], 'body': base64.b64encode(data).decode('ascii')}
        finally:
            response['Body'].close()

    def _save(self, entry: Entry, key: str, value: dict) -> None:
        path = self._cache_path(entry, key)
        data = json.dumps(value).encode()
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data)

    def _accept_metadata(self, entry: Entry, value: dict, startup: bool) -> None:
        metadata = parse_manifest(json.loads(base64.b64decode(value['body'])))
        archive_id = metadata['id']
        if entry.metadata and entry.metadata['id'] != archive_id:
            raise ValueError('archive ID changed; restart Navigator to adopt a new identity')
        other = self.claims.get(archive_id)
        if other is not None and other is not entry:
            if startup:
                raise DuplicateArchiveError(f'duplicate archive ID: {archive_id}')
            raise ValueError(f'duplicate archive ID during recovery: {archive_id}')
        self._save(entry, 'archive.json', value)
        self.claims[archive_id] = entry
        entry.metadata = metadata

    def _images(self, entry: Entry) -> None:
        for role in ('logo', 'preview'):
            image = entry.metadata.get(role)
            if image is None:
                entry.images.pop(role, None)
                continue
            key = image['src']
            try:
                value = self._object(entry, key, 8 * 1024 * 1024)
                self._publish_image(entry, role, image, value)
                self._save(entry, key, value)
            except Exception as error:
                print(f'WARNING: {entry.source.bucket} {role}: {error}', file=sys.stderr)
                cached = self._cached(entry, key)
                if cached:
                    try:
                        self._publish_image(entry, role, image, cached)
                    except (ValueError, OSError):
                        pass

    def _publish_image(self, entry: Entry, role: str, image: dict, value: dict) -> None:
        data = base64.b64decode(value['body'])
        extension = image_extension(data)
        name = hashlib.sha256(data).hexdigest() + '.' + extension
        path = self.runtime / 'static' / 'catalog' / name
        if not path.exists():
            atomic_write(path, data)
        entry.images[role] = {'src': 'static/catalog/' + name, 'alt': image['alt']}

    def refresh(self, *, startup: bool = False) -> None:
        for entry in self.entries:
            errors = []
            try:
                if entry.store is None:
                    entry.store = RemoteArchiveStore(entry.source, self.cache, self.poll_seconds)
                # Do not hide a layout migration requirement behind cached metadata.
                try:
                    entry.store.check_legacy_layout()
                except LegacyLayoutError:
                    raise
                except Exception as error:
                    errors.append(str(error))
                try:
                    value = self._object(entry, 'archive.json', 1024 * 1024)
                    self._accept_metadata(entry, value, startup)
                except DuplicateArchiveError:
                    raise
                except Exception as error:
                    errors.append(str(error))
                    if entry.metadata is None:
                        value = self._cached(entry, 'archive.json')
                        if value:
                            self._accept_metadata(entry, value, startup)
                    if entry.metadata is None:
                        raise ValueError('no valid archive.json: ' + str(error)) from error
                self._images(entry)
                archive_id = entry.metadata['id']
                if entry.archive is None:
                    entry.archive = entry.store.load_archive(archive_id)
                    if entry.store.stale:
                        errors.append('Using cached index; remote refresh failed')
                else:
                    entry.store._poll_archive(archive_id)
                entry.status = 'stale' if errors else 'ready'
            except DuplicateArchiveError:
                raise
            except Exception as error:
                errors.append(str(error))
                if isinstance(error, LegacyLayoutError):
                    entry.archive = None
                entry.status = 'stale' if entry.archive else 'unavailable'
            entry.error = '; '.join(errors)
            if entry.error:
                print(f'WARNING: {entry.source.bucket}/{entry.source.prefix}: {entry.error}', file=sys.stderr)
        self.publish()

    def publish(self) -> None:
        cards = []
        archives = []
        for entry in self.entries:
            card = dict(entry.metadata or {'name': entry.source.bucket + ('/' + entry.source.prefix if entry.source.prefix else '')})
            card.pop('logo', None)
            card.pop('preview', None)
            card.update(entry.images)
            card['status'] = entry.status
            if entry.archive:
                archives.append(entry.archive)
                stat = entry.archive.replay_index.stat()
                stamp = (stat.st_mtime_ns, stat.st_size, entry.metadata['homepage'],
                         json.dumps(entry.metadata.get('featured_capture')))
                if stamp != entry.summary_stamp:
                    entry.summary = capture_summary(entry.archive, entry.metadata)
                    entry.summary_stamp = stamp
                card.update(entry.summary)
            cards.append(card)
        state = {'title': self.config.title, 'archives': cards,
                 'collections': build_config(archives, wayback_fallback=self.fallback)['collections']}
        path = self.runtime / 'catalog-state.json'
        data = json.dumps(state).encode()
        if not path.exists() or path.read_bytes() != data:
            atomic_write(path, data)

    def start_polling(self) -> None:
        self._thread = threading.Thread(target=self._poll, daemon=True, name='archive-magic-catalog')
        self._thread.start()

    def _poll(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.refresh()
            except Exception as error:
                print(f'WARNING: catalog refresh failed: {error}', file=sys.stderr)

    def stop_polling(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class DuplicateArchiveError(ValidationError):
    pass


def capture_summary(archive: Archive, metadata: dict) -> dict:
    featured = metadata.get('featured_capture')
    target = canonicalize(featured['url'] if featured else metadata['homepage'])
    desired = datetime.strptime(featured['timestamp'], '%Y%m%d%H%M%S') if featured else None
    first = last = chosen = None
    chosen_url = None
    best = None
    with archive.replay_index.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            key, timestamp, payload = line.split(' ', 2)
            first = min(first, timestamp) if first else timestamp
            last = max(last, timestamp) if last else timestamp
            if key != target:
                continue
            moment = datetime.strptime(timestamp, '%Y%m%d%H%M%S')
            score = (abs((moment - desired).total_seconds()), timestamp) if desired else (-int(timestamp), timestamp)
            if best is None or score < best:
                best, chosen = score, timestamp
                try:
                    chosen_url = web_url(json.loads(payload).get('url'), 'capture URL')
                except ValueError:
                    chosen_url = featured['url'] if featured else metadata['homepage']
    search = quote(archive.archive_id, safe='') + '/'
    replay = search + chosen + '/' + quote(chosen_url, safe=':/?=&;%+@,~!*()') if chosen else search
    return {'first_capture': _date(first), 'last_capture': _date(last),
            'domain': urlsplit(metadata['homepage']).hostname, 'replay': replay, 'search': search,
            'search_fallback': chosen is None}


def _date(timestamp: str | None) -> str:
    return f'{timestamp[:4]}-{timestamp[4:6]}-{timestamp[6:8]}' if timestamp else ''
