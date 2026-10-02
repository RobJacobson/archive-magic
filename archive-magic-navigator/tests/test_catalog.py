import io
import json
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from archive_magic_navigator.errors import ValidationError
from archive_magic_navigator.catalog import CatalogStore, DuplicateArchiveError
from archive_magic_navigator.settings import load_catalog, parse_manifest
from test_settings_remote import FakeRemoteS3


class Buckets:
    def __init__(self):
        self.buckets = {}
        self.gets = []
        self.downloads = []
        self.offline = set()

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeRemoteS3())

    def list_objects_v2(self, **kwargs):
        if kwargs['Bucket'] in self.offline:
            raise OSError('offline')
        return self.bucket(kwargs['Bucket']).list_objects_v2(**kwargs)

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        if kwargs['Bucket'] in self.offline:
            raise OSError('offline')
        fake = self.bucket(kwargs['Bucket'])
        item = fake.objects.get(kwargs['Key'])
        if item and kwargs.get('IfNoneMatch') == item['etag']:
            raise ClientError({'Error': {'Code': '304'}}, 'GetObject')
        self.downloads.append(kwargs)
        return fake.get_object(**kwargs)

    def head_object(self, **kwargs):
        if kwargs['Bucket'] in self.offline:
            raise OSError('offline')
        item = self.bucket(kwargs['Bucket']).objects.get(kwargs['Key'])
        if item is None:
            raise ClientError({'Error': {'Code': '404'}}, 'HeadObject')
        return {'ETag': item['etag']}


def seed(remote, bucket='one', archive_id='example.org', prefix=''):
    fake = remote.bucket(bucket)
    root = prefix + '/' if prefix else ''
    manifest = {'id': archive_id, 'name': 'Example Organization', 'homepage': 'http://example.org/',
                'logo': {'src': 'assets/logo.png', 'alt': 'Example logo'},
                'preview': {'src': 'assets/preview.gif', 'alt': 'Homepage'}}
    fake.seed(root + 'archive.json', json.dumps(manifest).encode())
    fake.seed(root + 'assets/logo.png', b'\x89PNG\r\n\x1a\n' + b'x'*16)
    fake.seed(root + 'assets/preview.gif', b'GIF89a' + b'x'*16)
    fake.seed(root + f'data/{archive_id}-2020-001.warc.gz', b'x'*100)
    lines = []
    for timestamp in ('20200101000000', '20200103000000'):
        lines.append('org,example)/ ' + timestamp + ' ' + json.dumps({'url': 'http://example.org/', 'filename': f'{archive_id}-2020-001.warc.gz', 'offset': '0', 'length': '20'}))
    fake.seed(root + f'data/{archive_id}-2020-index.cdxj', ('\n'.join(lines)+'\n').encode())
    return manifest


def manager(tmp_path, monkeypatch, remote, entries=None):
    monkeypatch.setattr('archive_magic_navigator.remote.boto3.client', lambda *a, **k: remote)
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps({'title': 'Our Archive', 'storage': {'endpoint_url': 'https://example.invalid', 'region': 'auto'}, 'archives': entries or [{'bucket': 'one'}]}))
    return CatalogStore(load_catalog(path), tmp_path/'cache', tmp_path/'runtime', 300, False)


def snapshot(store):
    return json.loads((store.runtime/'catalog-state.json').read_text())


def test_independent_buckets_images_and_capture_selection(tmp_path, monkeypatch):
    remote = Buckets()
    manifest = seed(remote)
    manifest['featured_capture'] = {'url': 'http://example.org/', 'timestamp': '20200102000000'}
    remote.bucket('one').seed('archive.json', json.dumps(manifest).encode())
    seed(remote, 'two', 'second', 'archive')
    store = manager(tmp_path, monkeypatch, remote, [{'bucket':'one'}, {'bucket':'two', 'prefix':'archive'}])
    store.refresh(startup=True)
    state = snapshot(store)
    assert [a['id'] for a in state['archives']] == ['example.org', 'second']
    first, second = state['archives']
    assert '20200101000000' in first['replay']  # Earlier tie.
    assert '20200103000000' in second['replay']
    assert first['first_capture'] == '2020-01-01'
    assert first['last_capture'] == '2020-01-03'
    assert (store.runtime/first['logo']['src']).is_file()
    assert first['preview']['alt'] == 'Homepage'
    assert state['collections']['second']['archive_paths'] == ['s3://two/archive/data/']
    assert not list(store.cache.rglob('*.warc.gz'))
    assert not any(call['Key'].endswith('.warc.gz') for call in remote.gets)
    downloaded = len(remote.downloads)
    store.refresh()
    assert len(remote.downloads) == downloaded
    assert all(a['status'] == 'ready' for a in snapshot(store)['archives'])
    assert not any('IfNoneMatch' in call for call in remote.gets)


def test_failure_recovery_metadata_updates_and_stale_cache(tmp_path, monkeypatch):
    remote = Buckets()
    seed(remote)
    store = manager(tmp_path, monkeypatch, remote, [{'bucket':'one'}, {'bucket':'two'}])
    store.refresh(startup=True)
    assert [a['status'] for a in snapshot(store)['archives']] == ['ready', 'unavailable']
    manifest = seed(remote, 'two', 'second')
    store.refresh()
    assert len(snapshot(store)['collections']) == 2
    manifest['name'] = '<New name>'
    remote.bucket('two').seed('archive.json', json.dumps(manifest).encode())
    store.refresh()
    assert snapshot(store)['archives'][1]['name'] == '<New name>'
    remote.bucket('two').seed('archive.json', b'broken')
    store.refresh()
    assert snapshot(store)['archives'][1]['name'] == '<New name>'
    assert snapshot(store)['archives'][1]['status'] == 'stale'
    remote.offline = {'one', 'two'}
    restarted = manager(tmp_path, monkeypatch, remote, [{'bucket':'one'}, {'bucket':'two'}])
    restarted.refresh(startup=True)
    assert len(snapshot(restarted)['collections']) == 2
    assert all(a['status'] == 'stale' for a in snapshot(restarted)['archives'])


def test_duplicate_ids_startup_and_recovery(tmp_path, monkeypatch):
    remote = Buckets()
    seed(remote)
    seed(remote, 'two')
    store = manager(tmp_path, monkeypatch, remote, [{'bucket':'one'}, {'bucket':'two'}])
    with pytest.raises(DuplicateArchiveError):
        store.refresh(startup=True)
    remote.bucket('two').objects.clear()
    store = manager(tmp_path, monkeypatch, remote, [{'bucket':'one'}, {'bucket':'two'}])
    store.refresh(startup=True)
    seed(remote, 'two')
    store.refresh()
    assert snapshot(store)['archives'][1]['status'] == 'unavailable'
    assert len(snapshot(store)['collections']) == 1


def test_missing_image_search_fallback_and_identity_freeze(tmp_path, monkeypatch):
    remote = Buckets()
    manifest = seed(remote)
    manifest['homepage'] = 'https://missing.example/'
    remote.bucket('one').seed('archive.json', json.dumps(manifest).encode())
    remote.bucket('one').objects.pop('assets/logo.png')
    store = manager(tmp_path, monkeypatch, remote)
    store.refresh(startup=True)
    card = snapshot(store)['archives'][0]
    assert 'logo' not in card
    assert card['status'] == 'ready'
    assert card['search_fallback']
    assert card['replay'] == 'example.org/'
    manifest['id'] = 'changed'
    remote.bucket('one').seed('archive.json', json.dumps(manifest).encode())
    store.refresh()
    assert snapshot(store)['archives'][0]['id'] == 'example.org'
    assert snapshot(store)['archives'][0]['status'] == 'stale'


def test_legacy_layout_never_hidden_by_cached_data(tmp_path, monkeypatch):
    remote = Buckets()
    seed(remote)
    store = manager(tmp_path, monkeypatch, remote)
    store.refresh(startup=True)
    remote.bucket('one').seed('example.org-2020-001.warc.gz', b'x')
    store.refresh()
    assert snapshot(store)['archives'][0]['status'] == 'unavailable'
    assert 'legacy flat' in store.entries[0].error


def test_cache_isolation_by_bucket_prefix_endpoint(tmp_path):
    from archive_magic_navigator.settings import RemoteSource
    from dataclasses import replace
    source = RemoteSource('one')
    assert len({source.cache_key, replace(source, bucket='two').cache_key,
                replace(source, prefix='other').cache_key,
                replace(source, endpoint_url='https://other').cache_key}) == 4


@pytest.mark.parametrize('src', ['../secret', '/root', 'https://host/image.png', 'a/../../b', 'a\\b', 'a/%2e%2e/b', 'a//b'])
def test_manifest_asset_containment(src):
    with pytest.raises((ValueError, ValidationError)):
        parse_manifest({'id':'x', 'name':'X', 'homepage':'https://x.test', 'logo':{'src':src, 'alt':'X'}})


@pytest.mark.parametrize('change', [{'schema_version':1}, {'id':'bad/id'}, {'homepage':'javascript:bad'}, {'logo':{'src':'assets/x.png'}}, {'preview':{'src':'assets/x.png', 'alt':None}}, {'featured_capture':{'url':'https://x.test', 'timestamp':'20201301000000'}}])
def test_manifest_validation(change):
    with pytest.raises((ValueError, ValidationError)):
        parse_manifest({'id':'x', 'name':'X', 'homepage':'https://x.test', **change})


def test_catalog_validation_and_defaults(tmp_path):
    path = tmp_path/'catalog.json'
    path.write_text(json.dumps({'archives':[{'bucket':'one'}, {'bucket':'two'}]}))
    config = load_catalog(path)
    assert [a.bucket for a in config.archives] == ['one','two']
    assert config.archives[0].endpoint_url is None
    assert config.archives[0].region == 'us-east-1'
    for value in ({'archives':[{'bucket':'one', 'name':'duplicate metadata'}]}, {'schema_version':1, 'archives':[]}, {'archives':[{'bucket':'one'}, {'bucket':'one'}]}):
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError):
            load_catalog(path)


def test_image_refresh_replaces_accepted_asset_but_invalid_image_does_not(tmp_path, monkeypatch):
    remote = Buckets()
    seed(remote)
    store = manager(tmp_path, monkeypatch, remote)
    store.refresh(startup=True)
    old = snapshot(store)['archives'][0]['logo']['src']
    remote.bucket('one').seed('assets/logo.png', b'GIF89a' + b'new image')
    store.refresh()
    new = snapshot(store)['archives'][0]['logo']['src']
    assert new != old and new.endswith('.gif')
    remote.bucket('one').seed('assets/logo.png', b'<html>not an image</html>')
    store.refresh()
    assert snapshot(store)['archives'][0]['logo']['src'] == new
    assert snapshot(store)['archives'][0]['status'] == 'ready'


def test_failed_snapshot_publication_preserves_frontend_state(tmp_path, monkeypatch):
    import archive_magic_navigator.catalog as catalog_module
    remote = Buckets()
    manifest = seed(remote)
    store = manager(tmp_path, monkeypatch, remote)
    store.refresh(startup=True)
    old = (store.runtime/'catalog-state.json').read_bytes()
    manifest['name'] = 'Updated'
    remote.bucket('one').seed('archive.json', json.dumps(manifest).encode())
    real_replace = catalog_module.os.replace
    def fail_snapshot(source, destination):
        if Path(destination).name == 'catalog-state.json':
            raise OSError('interrupted publication')
        return real_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(catalog_module.os, 'replace', fail_snapshot)
        with pytest.raises(OSError):
            store.refresh()
    assert (store.runtime/'catalog-state.json').read_bytes() == old
    assert not list(store.runtime.rglob('.tmp-*'))
    store.refresh()
    assert snapshot(store)['archives'][0]['name'] == 'Updated'


def test_capture_summary_tolerates_blank_rows_and_missing_original_url(tmp_path, monkeypatch):
    remote = Buckets()
    seed(remote)
    fake = remote.bucket('one')
    key = 'data/example.org-2020-index.cdxj'
    index = fake.objects[key]['body'].replace(b'"url": "http://example.org/"', b'"url": null')
    fake.seed(key, b'\n' + index + b'\n')
    store = manager(tmp_path, monkeypatch, remote)
    store.refresh(startup=True)
    card = snapshot(store)['archives'][0]
    assert card['status'] == 'ready'
    assert card['replay'] == 'example.org/20200103000000/http://example.org/'
