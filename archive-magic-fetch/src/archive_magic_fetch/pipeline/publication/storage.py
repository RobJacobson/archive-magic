"""Explicit bucket lifecycle. Local absence is never a remote deletion request.

One writer per bucket/prefix is required. Receipts bind pending uploads to an
observed remote baseline; they are not a distributed lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import boto3
from botocore.config import Config

from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.archive.validate_local_archive import validate_local_archive
from archive_magic_fetch.config.presentation import metadata, asset_path
from archive_magic_fetch.models import PublicationError
from archive_magic_fetch.runtime.manage_archive_files import file_sha256, write_json_durably

ACTIVE = ContextVar('archive_storage', default=None)


def completed_discovery(path):
    store = ACTIVE.get()
    if store is not None:
        store.publish_files([Path(path)])


@contextmanager
def active_storage(store):
    token = ACTIVE.set(store)
    try:
        yield
    finally:
        ACTIVE.reset(token)


def atomic_json(path, value):
    write_json_durably(path, value)


class BucketStorage:
    def __init__(self, output, archive_id, *, client=None):
        self.output = output
        self.archive_id = archive_id
        self.root = output.data_directory.parent.resolve()
        for name in ('data', 'discovery', 'logs', '.state'):
            directory = self.root / name
            if directory.is_symlink() or not directory.resolve().is_relative_to(self.root):
                raise PublicationError('output directories must be contained, without symlinks')
        self.layout = ArchiveLayout(output.data_directory, archive_id)
        self.client = client if client is not None else boto3.client('s3', endpoint_url=output.endpoint_url, region_name=output.region, config=Config(connect_timeout=10, read_timeout=60, retries={'mode': 'standard', 'total_max_attempts': 3}))
        self.prefix = output.prefix.strip('/')
        self.receipt = self.root / '.state' / 'publication.json'
        self.binding = {'bucket': output.bucket, 'prefix': self.prefix, 'endpoint': output.endpoint_url, 'region': output.region, 'id': archive_id}
        self.state = {'version': 1, 'binding': self.binding, 'baseline': {}, 'pending': {}}
        if self.receipt.exists():
            self.state = json.loads(self.receipt.read_text())
            if self.state.get('version') != 1 or self.state.get('binding') != self.binding:
                raise PublicationError('publication state belongs to a different collection or bucket')

    def key(self, relative):
        return '/'.join(p for p in (self.prefix, relative) if p)

    def managed(self, relative):
        parts = relative.split('/')
        if any(p in {'', '.', '..'} for p in parts) or '\\' in relative:
            return False
        if relative.startswith('discovery/'):
            return bool(re.fullmatch(
                r'discovery/(?:wayback/v1/[0-9a-f]{64}/[0-9]{4}\.cdx\.json|'
                r'common-crawl/v1/[0-9a-f]{64}/[A-Za-z0-9._-]+/[0-9]{4}\.json)',
                relative,
            ))
        return bool(re.fullmatch(r'data/' + re.escape(self.archive_id) + r'-\d{4}-(?:\d{3,}\.warc\.gz|index\.cdxj)', relative))

    def inventory(self):
        result = {}
        prefix = self.key('')
        if prefix:
            prefix += '/'
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.output.bucket, Prefix=prefix):
            for item in page.get('Contents', []):
                relative = item['Key'][len(prefix):]
                if '/' not in relative and relative.endswith(('.warc.gz', '.cdxj')):
                    raise PublicationError('legacy flat bucket layout; migrate root archive files into data/')
                if self.managed(relative):
                    result[relative] = {'etag': item['ETag'], 'size': item['Size'], 'modified': str(item.get('LastModified', ''))}
        return result

    def local(self, relative):
        path = self.root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise PublicationError('archive path escapes local output')
        return path

    def remote_hash(self, relative, signature, destination=None):
        response = self.client.get_object(Bucket=self.output.bucket, Key=self.key(relative), IfMatch=signature['etag'])
        body = response['Body']
        digest = hashlib.sha256()
        total = 0
        stream = destination.open('wb') if destination else None
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                digest.update(chunk)
                total += len(chunk)
                if stream:
                    stream.write(chunk)
        finally:
            body.close()
            if stream:
                stream.close()
        if total != signature['size']:
            raise PublicationError(f'incomplete remote object: {relative}')
        return digest.hexdigest()

    def save(self):
        atomic_json(self.receipt, self.state)

    def preflight(self, *, reset=False):
        """Verify baseline and recover completed uploads before any acquisition."""
        self.recover_generation()
        remote = self.inventory()
        if reset:
            self.state['baseline'] = {k: v for k, v in self.state['baseline'].items() if not k.startswith('data/')}
            self.state['pending'] = {k: v for k, v in self.state['pending'].items() if not k.startswith('data/')}
            remote = {k: v for k, v in remote.items() if not k.startswith('data/')}
        baseline = self.state['baseline']
        pending = self.state['pending']
        for relative, old in list(baseline.items()):
            if relative not in remote:
                raise PublicationError(f'remote object removed: {relative}; resolve conflict before publication')
        for relative, signature in remote.items():
            path = self.local(relative)
            if not path.is_file():
                raise PublicationError(f'local copy missing: {relative}; run --restore')
            actual = file_sha256(path)
            old = baseline.get(relative)
            intended = pending.get(relative)
            if old and old['remote'] == signature:
                allowed = {old['sha256']}
                if intended:
                    allowed.add(intended)
                if actual not in allowed:
                    raise PublicationError(f'unrecorded local changes: {relative}; preserve them and resolve conflict')
                continue
            remote_digest = self.remote_hash(relative, signature)
            if old and (not intended or remote_digest != intended):
                raise PublicationError(f'remote object changed: {relative}; resolve conflict before publication')
            if actual != remote_digest:
                raise PublicationError(f'local/remote conflict: {relative}; preserve local work before --restore')
            baseline[relative] = {'remote': signature, 'sha256': remote_digest}
            pending.pop(relative, None)
        for relative, digest in pending.items():
            path = self.local(relative)
            if not path.is_file() or file_sha256(path) != digest:
                raise PublicationError(f'pending publication changed or missing: {relative}')
        self.save()

    def assert_remote_unchanged(self):
        expected = {key: value['remote'] for key, value in self.state['baseline'].items()}
        if self.inventory() != expected:
            raise PublicationError('bucket changed since preflight; retry to recover or resolve conflict')

    def files(self):
        result = []
        for directory in ('data', 'discovery'):
            base = self.root / directory
            if base.is_symlink():
                raise PublicationError('output subdirectories must not be symlinks')
            if base.exists():
                for path in base.rglob('*'):
                    if path.is_symlink():
                        raise PublicationError('output must not contain symlinks')
                    if path.is_file() and self.managed(path.relative_to(self.root).as_posix()):
                        result.append(path)
        return result

    def validate_discovery(self, path):
        from archive_magic_fetch.pipeline.discovery.cache import validate_cache
        validate_cache(path, path.relative_to(self.root / 'discovery').as_posix())

    def publish_files(self, paths):
        paths = list(paths)
        if not paths:
            return
        self.assert_remote_unchanged()
        for path in paths:
            relative = path.relative_to(self.root).as_posix()
            if not self.managed(relative):
                raise PublicationError(f'unmanaged publication path: {relative}')
            self.local(relative)
            if relative.startswith('discovery/'):
                self.validate_discovery(path)
            self.state['pending'][relative] = file_sha256(path)
        # Persist all intended hashes before the first upload for crash recovery.
        self.save()
        for path in paths:
            relative = path.relative_to(self.root).as_posix()
            self.assert_remote_unchanged()
            digest = self.state['pending'][relative]
            if file_sha256(path) != digest:
                raise PublicationError('local file changed during publication')
            old = self.state['baseline'].get(relative)
            if not old or old['sha256'] != digest:
                self.client.upload_file(str(path), self.output.bucket, self.key(relative))
                inventory = self.inventory()
                signature = inventory.get(relative)
                if signature is None or self.remote_hash(relative, signature) != digest:
                    raise PublicationError(f'upload verification failed: {relative}')
                self.state['baseline'][relative] = {'remote': signature, 'sha256': digest}
            self.state['pending'].pop(relative)
            self.save()

    def record_generation(self, paths):
        """Persist validated staged bytes before their recoverable local promotion."""
        self.assert_remote_unchanged()
        self.state['generation'] = {'previous': dict(self.state['pending']), 'files': {'data/' + path.name: file_sha256(path) for path in paths}}
        self.state['pending'].update(self.state['generation']['files'])
        self.save()

    def finish_generation(self):
        self.state.pop('generation', None)
        self.save()

    def recover_generation(self):
        generation = self.state.get('generation')
        if generation is None:
            return
        actual = {key: file_sha256(self.local(key)) if self.local(key).is_file() else None for key in generation['files']}
        if all(actual[key] == digest for key, digest in generation['files'].items()):
            self.finish_generation()
        elif all(actual[key] == self.state['baseline'].get(key, {}).get('sha256') for key in generation['files']):
            # Promotion never started. Acquisition remains private and can resume;
            # canonical files retain the previous publication intentions.
            self.state['pending'] = generation['previous']
            self.finish_generation()
        else:
            raise PublicationError('incomplete local promotion; recover staging before publication')

    def publish(self, year=None):
        data = [p for p in self.files() if p.parent == self.layout.root and (year is None or p.name.startswith(f'{self.archive_id}-{year}-'))]
        if data:
            validate_local_archive(self.layout, year=year, allow_unindexed=True)
            from archive_magic_fetch.archive.format import parse_cdxj_line
            indexes = [p for p in data if p.suffix == '.cdxj']
            referenced = {parse_cdxj_line(line)[2]['filename'] for p in indexes for line in p.read_text().splitlines() if line}
            for path in data:
                if path.suffix != '.cdxj' and path.name not in referenced and 'data/' + path.name not in self.state['baseline']:
                    raise PublicationError(f'unindexed unpublished WARC: {path.name}')
            data = [p for p in data if p.suffix == '.cdxj' or p.name in referenced]
        discovery = [p for p in self.files() if p.is_relative_to(self.root / 'discovery')]
        self.publish_files(discovery)
        # WARCs are installed before any index points to their new bytes.
        self.publish_files(sorted(data, key=lambda p: (p.suffix == '.cdxj', p.name)))

    def restore(self):
        self.unresolved()
        inventory = self.inventory()
        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / '.state' / 'restore'
        if staging.is_symlink():
            raise PublicationError('restore staging must not be a symlink')
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix='download-', dir=staging) as name:
            stage = Path(name)
            hashes = {}
            for relative, signature in inventory.items():
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                hashes[relative] = self.remote_hash(relative, signature, target)
                local = self.local(relative)
                if local.exists() and (not local.is_file() or file_sha256(local) != hashes[relative]):
                    raise PublicationError(f'restore would overwrite local work: {relative}')
                if relative.startswith('discovery/'):
                    from archive_magic_fetch.pipeline.discovery.cache import validate_cache
                    validate_cache(target, relative.removeprefix('discovery/'))
            if any(key.startswith('data/') for key in inventory):
                from archive_magic_fetch.pipeline.write_captures import _validate_warc
                for path in (stage / 'data').glob('*.warc.gz'):
                    _validate_warc(path)
                validate_local_archive(ArchiveLayout(stage / 'data', self.archive_id), allow_unindexed=True)
            if self.inventory() != inventory:
                raise PublicationError('bucket changed during restore; retry')
            for relative in inventory:
                target = self.local(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.replace(stage / relative, target)
            self.state['baseline'] = {key: {'remote': sig, 'sha256': hashes[key]} for key, sig in inventory.items()}
            self.state['pending'] = {}
            self.save()

    def unresolved(self):
        discovery_work = self.root / '.state' / 'discovery'
        if ((self.layout.root / '.staging').exists() or self.state['pending'] or self.state.get('generation')
                or (discovery_work.exists() and any(p.is_file() for p in discovery_work.rglob('*')))):
            raise PublicationError('unresolved transaction; finish or retry publication before restore/eviction')

    def evict(self):
        self.unresolved()
        restore_stage = self.root / '.state' / 'restore'
        if restore_stage.exists() and any(restore_stage.iterdir()):
            raise PublicationError('unfinished restore; retry --restore before eviction')
        inventory = self.inventory()
        files = self.files()
        if any(p.parent == self.layout.root for p in files):
            validate_local_archive(self.layout, allow_unindexed=True)
        if self.root.exists():
            for path in self.root.iterdir():
                if path.name not in {'data', 'discovery', 'logs', '.state'} or path.is_symlink():
                    raise PublicationError(f'unknown output entry; preserve it before eviction: {path}')
        # Unknown files are not implicitly discarded with the archive output.
        for base in (self.root / 'data', self.root / 'discovery'):
            if base.exists():
                for path in base.rglob('*'):
                    if path.is_file() and path not in files:
                        raise PublicationError(f'unpublished or temporary output: {path}')
        for path in files:
            relative = path.relative_to(self.root).as_posix()
            signature = inventory.get(relative)
            if signature is None or self.remote_hash(relative, signature) != file_sha256(path):
                raise PublicationError(f'no verified bucket copy: {relative}')
            if relative.startswith('discovery/'):
                self.validate_discovery(path)
        if self.inventory() != inventory:
            raise PublicationError('bucket changed during eviction verification')
        if self.root.exists():
            shutil.rmtree(self.root)

    def reset_receipt(self):
        # Explicit reset removes only data; discovery still has its baseline.
        self.state['baseline'] = {k: v for k, v in self.state['baseline'].items() if not k.startswith('data/')}
        self.state['pending'] = {k: v for k, v in self.state['pending'].items() if not k.startswith('data/')}
        self.save()

    def publish_metadata(self, config):
        document = metadata(config.presentation, required=True)
        encoded = json.dumps(document, indent=2).encode() + b'\n'
        if len(encoded) > 1024 * 1024:
            raise PublicationError('presentation manifest exceeds 1 MiB')
        images = []
        for role in ('logo', 'preview'):
            if role in document:
                src = document[role]['src']
                path = asset_path(config.collection_directory, src)
                data = path.read_bytes()
                valid = data.startswith((b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF87a', b'GIF89a')) or (data.startswith(b'RIFF') and data[8:12] == b'WEBP')
                if len(data) > 8 * 1024 * 1024 or not valid:
                    raise PublicationError('presentation image must be PNG/JPEG/GIF/WebP, at most 8 MiB')
                images.append((src, data))
        for src, data in images:
            self.client.put_object(Bucket=self.output.bucket, Key=self.key(src), Body=data)
        self.client.put_object(Bucket=self.output.bucket, Key=self.key('archive.json'), Body=encoded, ContentType='application/json')
