"""Strict, unversioned JSON catalog and bucket manifest contracts."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from .collections import validate_archive_id


@dataclass(frozen=True)
class RemoteSource:
    bucket: str
    prefix: str = ""
    endpoint_url: str | None = None
    region: str = "auto"

    def key(self, relative: str) -> str:
        return "/".join(p for p in (self.prefix, relative) if p)

    @property
    def cache_key(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class CatalogConfig:
    title: str
    archives: tuple[RemoteSource, ...]
    path: Path


def fields(value, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    extra = value.keys() - allowed
    if extra:
        raise ValueError(f"{label}: unknown fields: {', '.join(sorted(extra))}")
    return value


def text(value, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be {'a' if empty else 'a nonempty'} text string")
    return value


def object_key(value, label: str, *, empty: bool = False) -> str:
    value = text(value, label, empty=empty)
    if (value and any(p in {'', '.', '..'} for p in value.split('/'))) or any(c in value for c in '\\:%?#'):
        raise ValueError(f"{label} must be a contained bucket-relative object key")
    return value


def web_url(value, label: str) -> str:
    value = text(value, label)
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an HTTP(S) URL without credentials")
    return value


def parse_manifest(value) -> dict:
    value = fields(value, {'id', 'name', 'description', 'homepage', 'logo', 'preview', 'featured_capture'}, 'archive.json')
    result = {
        'id': validate_archive_id(text(value.get('id'), 'id')),
        'name': text(value.get('name'), 'name'),
        'homepage': web_url(value.get('homepage'), 'homepage'),
    }
    if 'description' in value:
        result['description'] = text(value['description'], 'description', empty=True)
    for role in ('logo', 'preview'):
        if role in value:
            entry = fields(value[role], {'src', 'alt'}, role)
            result[role] = {'src': object_key(entry.get('src'), f'{role}.src'),
                            'alt': text(entry.get('alt'), f'{role}.alt', empty=True)}
    if 'featured_capture' in value:
        entry = fields(value['featured_capture'], {'url', 'timestamp'}, 'featured_capture')
        timestamp = text(entry.get('timestamp'), 'featured_capture.timestamp')
        if not re.fullmatch(r'\d{14}', timestamp):
            raise ValueError('featured_capture.timestamp must contain 14 digits')
        datetime.strptime(timestamp, '%Y%m%d%H%M%S')
        result['featured_capture'] = {'url': web_url(entry.get('url'), 'featured_capture.url'), 'timestamp': timestamp}
    return result


def load_catalog(value: Path | str) -> CatalogConfig:
    path = Path(value).expanduser().resolve()
    if path.is_dir() or path.suffix.lower() == '.toml':
        raise ValueError('Navigator now requires --catalog catalog.json; migrate navigator.toml and directory catalogs')
    try:
        document = fields(json.loads(path.read_text()), {'title', 'storage', 'archives'}, 'catalog')
        storage = fields(document.get('storage', {}), {'endpoint_url', 'region'}, 'storage')
        endpoint = storage.get('endpoint_url')
        if endpoint is not None:
            endpoint = web_url(endpoint, 'storage.endpoint_url')
        region = text(storage.get('region', 'auto' if endpoint else 'us-east-1'), 'storage.region')
        entries = document.get('archives')
        if not isinstance(entries, list):
            raise ValueError('archives must be an array')
        sources = []
        for entry in entries:
            entry = fields(entry, {'bucket', 'prefix'}, 'archives entry')
            bucket = text(entry.get('bucket'), 'bucket')
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', bucket):
                raise ValueError('invalid bucket name')
            source = RemoteSource(bucket, object_key(entry.get('prefix', ''), 'prefix', empty=True), endpoint, region)
            if source in sources:
                raise ValueError(f'duplicate archive location: {bucket}/{source.prefix}')
            sources.append(source)
        return CatalogConfig(text(document.get('title', 'Website Archive'), 'title'), tuple(sources), path)
    except (OSError, ValueError) as error:
        raise ValueError(f'invalid catalog {path}: {error}') from error
