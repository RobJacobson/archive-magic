"""Bucket lifecycle safety with real WARC/index fixtures and opaque ETags."""
import json
import shutil
from pathlib import Path

import pytest
from bucket_helpers import Bucket
from helpers import make_collection, make_capt, make_source, playback
from archive_magic_fetch.config.models import FetchOutput, FetchConfig
from archive_magic_fetch.pipeline.publication.storage import BucketStorage, active_storage, completed_discovery
from archive_magic_fetch.pipeline.discovery.cache import wayback_path, wayback_document
from archive_magic_fetch.models import PublicationError


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / 'output'
    layout = make_collection(root / 'data')
    output = FetchOutput('remote', layout.root, 'bucket', 'prefix')
    bucket = Bucket()
    store = BucketStorage(output, 'example.org', client=bucket)
    store.preflight()
    return store, bucket


def cache(store):
    path = wayback_path(store.root / 'discovery', '*.example.org', 2004)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(wayback_document('*.example.org', 2004, [])))
    return path


def reload(store, bucket):
    return BucketStorage(store.output, store.archive_id, client=bucket)


def test_round_trip_publish_evict_restore(setup):
    store, bucket = setup
    discovery = cache(store)
    bucket.put('prefix/assets/logo.png', b'keep')
    bucket.put('prefix/data/another-site-2004-001.warc.gz', b'keep')
    store.publish()
    assert bucket.uploads[-1].endswith('-index.cdxj')
    assert any('/discovery/' in key for key in bucket.uploads)
    original = {p.relative_to(store.root): p.read_bytes() for p in store.files()}
    (store.root / 'logs').mkdir(exist_ok=True)
    (store.root / 'logs' / 'run.log').write_text('disposable')
    store.evict()
    assert not store.root.exists()
    with pytest.raises(PublicationError, match='--restore'):
        reload(store, bucket).preflight()
    restored = reload(store, bucket)
    restored.restore()
    assert {p.relative_to(store.root): p.read_bytes() for p in restored.files()} == original
    assert not (store.root / 'logs').exists()
    assert bucket.objects['prefix/assets/logo.png'] == b'keep'
    assert not (store.root / 'data' / 'another-site-2004-001.warc.gz').exists()
    restored.preflight()


@pytest.mark.parametrize('kind', ['missing', 'changed', 'remote_changed', 'remote_removed'])
def test_partial_local_or_remote_changes_refuse_publication(setup, kind):
    store, bucket = setup
    store.publish()
    path = next(store.layout.root.glob('*.warc.gz'))
    key = 'prefix/data/' + path.name
    if kind == 'missing':
        path.unlink()
    elif kind == 'changed':
        path.write_bytes(b'bad')
    elif kind == 'remote_changed':
        bucket.put(key, b'new remote data')
    else:
        del bucket.objects[key]
    before = dict(bucket.objects)
    with pytest.raises(PublicationError):
        reload(store, bucket).preflight()
    assert bucket.objects == before


def test_failed_upload_is_retryable_without_deletions(setup):
    store, bucket = setup
    bucket.fail = lambda key: key.endswith('.cdxj')
    with pytest.raises(OSError):
        store.publish()
    assert store.state['pending']
    bucket.fail = None
    retry = reload(store, bucket)
    retry.preflight()
    retry.publish()
    assert not retry.state['pending']
    assert any(k.endswith('.cdxj') for k in bucket.objects)


def test_crash_after_upload_before_receipt_is_recovered(setup, monkeypatch):
    store, bucket = setup
    upload = bucket.upload_file
    def interrupted(*args):
        upload(*args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(bucket, 'upload_file', interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.publish()
    monkeypatch.setattr(bucket, 'upload_file', upload)
    retry = reload(store, bucket)
    retry.preflight()
    retry.publish()
    assert not retry.state['pending']


def test_restore_conflict_preserves_all_local_files(setup):
    store, bucket = setup
    store.publish()
    files = store.files()
    files[0].write_bytes(b'unpublished local work')
    before = {p: p.read_bytes() for p in files}
    with pytest.raises(PublicationError, match='overwrite'):
        reload(store, bucket).restore()
    assert {p: p.read_bytes() for p in files} == before


def test_interrupted_restore_installs_no_partial_files(setup, monkeypatch):
    store, bucket = setup
    store.publish()
    shutil.rmtree(store.root)
    retry = reload(store, bucket)
    original = bucket.get_object
    calls = []
    def interrupted(**kw):
        calls.append(kw)
        if len(calls) == 2:
            raise OSError('download interrupted')
        return original(**kw)
    monkeypatch.setattr(bucket, 'get_object', interrupted)
    with pytest.raises(OSError):
        retry.restore()
    assert not list(store.root.rglob('*.warc.gz'))
    monkeypatch.setattr(bucket, 'get_object', original)
    retry.restore()
    retry.preflight()


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'pending', 'staging', 'unmanaged'])
def test_failed_eviction_preserves_local_output(setup, kind):
    store, bucket = setup
    store.publish()
    path = next(store.layout.root.glob('*.warc.gz'))
    if kind == 'missing':
        del bucket.objects['prefix/data/' + path.name]
    elif kind == 'corrupt':
        bucket.put('prefix/data/' + path.name, b'bad')
    elif kind == 'pending':
        store.state['pending']['data/' + path.name] = 'unpublished'
    elif kind == 'staging':
        (store.layout.root / '.staging').mkdir()
    else:
        (store.layout.root / 'important.txt').write_text('keep')
    with pytest.raises(PublicationError):
        store.evict()
    assert path.exists()


def test_missing_receipt_requires_content_match(setup):
    store, bucket = setup
    store.publish()
    store.receipt.unlink()
    retry = reload(store, bucket)
    retry.preflight()
    assert retry.state['baseline']
    store.receipt.unlink()
    next(store.layout.root.glob('*.warc.gz')).write_bytes(b'conflicting')
    with pytest.raises(PublicationError, match='conflict'):
        reload(store, bucket).preflight()


def test_discovery_only_publication_and_invalid_cache(setup):
    store, bucket = setup
    shutil.rmtree(store.layout.root)
    path = cache(store)
    with active_storage(store):
        completed_discovery(path)
    assert len(bucket.objects) == 1
    store.publish()
    path.write_text('[]')
    with pytest.raises(ValueError, match='envelope'):
        store.publish()


def test_metadata_matches_navigator_contract_and_upload_order(setup, tmp_path):
    # Import only in the workspace contract test; Fetch has no Navigator dependency.
    parse_manifest = pytest.importorskip("archive_magic_navigator.settings").parse_manifest
    store, bucket = setup
    root = tmp_path / 'collection'
    (root / 'assets').mkdir(parents=True)
    (root / 'assets' / 'logo.gif').write_bytes(b'GIF89a' + b'fixture')
    presentation = {'id': 'example.org', 'name': 'Example', 'homepage': 'https://example.org/', 'logo': {'src': 'assets/logo.gif', 'alt': 'Example'}}
    config = FetchConfig('example.org', '*.example.org', store.output, collection_directory=root, presentation=presentation)
    store.publish_metadata(config)
    assert bucket.uploads == ['prefix/assets/logo.gif', 'prefix/archive.json']
    assert parse_manifest(json.loads(bucket.objects['prefix/archive.json'])) == presentation
    assert not (root / 'archive.json').exists()


def test_metadata_requires_name_and_homepage(setup, tmp_path):
    store, bucket = setup
    config = FetchConfig('example.org', '*.example.org', store.output, collection_directory=tmp_path, presentation={'id': 'example.org'})
    with pytest.raises(ValueError, match='requires'):
        store.publish_metadata(config)
    assert not bucket.objects


def test_collection_lock_survives_eviction(setup, tmp_path):
    from archive_magic_fetch.runtime.manage_archive_files import archive_lock
    store, bucket = setup
    store.publish()
    definition = tmp_path / 'collection'
    with archive_lock(store.layout, definition):
        store.evict()
        with pytest.raises(PublicationError, match='another fetch'):
            with archive_lock(store.layout, definition):
                pass
    assert (definition / '.archive-magic.lock').exists()


def test_remote_changes_between_preflight_and_publish_are_rejected(setup):
    store, bucket = setup
    bucket.put('prefix/data/example.org-2005-index.cdxj', b'concurrent publication')
    with pytest.raises(PublicationError, match='changed'):
        store.publish()
    assert not bucket.uploads


def test_discovery_persists_despite_warc_failure(setup, monkeypatch):
    from archive_magic_fetch.config.build_settings import FetchSettings
    from archive_magic_fetch.models import CaptureRef
    from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import _CdxResult
    from archive_magic_fetch.pipeline.run_fetch import run_fetch
    from unittest.mock import MagicMock
    store, bucket = setup
    shutil.rmtree(store.root)
    monkeypatch.setattr('archive_magic_fetch.pipeline.publication.storage.boto3.client', lambda *a, **kw: bucket)
    monkeypatch.setattr('archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx._fetch_cdx', lambda **kw: _CdxResult((CaptureRef(make_capt(), 'text/html'),), 'example.org', 'domain'))
    def fail(*args, **kwargs):
        raise OSError('WARC failure')
    monkeypatch.setattr('archive_magic_fetch.pipeline.run_fetch.write_captures', fail)
    settings = FetchSettings('*.example.org', '20040101000000', '20041231235959', 'example.org', store.output)
    result = run_fetch(settings, source=make_source(settings, client_factory=MagicMock), sleep=lambda _: None)
    assert result.exit_code == 1
    assert any('/discovery/' in key for key in bucket.objects)
    assert not any('/data/' in key for key in bucket.objects)


def test_uncommitted_generation_is_discarded_on_recovery(setup, tmp_path):
    store, bucket = setup
    store.publish()
    staged = tmp_path / 'stage'
    staged.mkdir()
    old = next(store.layout.root.glob('*.warc.gz'))
    new = staged / old.name
    new.write_bytes(b'not yet promoted')
    store.record_generation([new])
    retry = reload(store, bucket)
    retry.preflight()
    assert not retry.state['pending']
    assert 'generation' not in retry.state


def test_promoted_generation_survives_crash_before_receipt(setup, tmp_path):
    store, bucket = setup
    store.publish()
    old = next(store.layout.root.glob('*.warc.gz'))
    staged = tmp_path / old.name
    staged.write_bytes(old.read_bytes() + b'new bytes')
    store.record_generation([staged])
    old.write_bytes(staged.read_bytes())
    retry = reload(store, bucket)
    retry.preflight()
    assert 'data/' + old.name in retry.state['pending']
    assert 'generation' not in retry.state
