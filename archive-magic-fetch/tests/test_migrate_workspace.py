import json
import tomllib
import pytest
from archive_magic_fetch.migrate_workspace import migrate, digest
from archive_magic_fetch.config.load_archive_config import load_config
from archive_magic_fetch.pipeline.discovery.cache import validate_cache


def legacy(tmp_path):
    source = tmp_path / 'old'
    site = source / 'example'
    (site / 'data').mkdir(parents=True)
    (site / 'index').mkdir()
    (site / 'index' / '2004.cdx.json').write_text('[]')
    (site / 'data' / 'payload').write_bytes(b'preserve byte for byte')
    (site / 'fetch.toml').write_text('[archive]\nid="example.org"\nurl_pattern="*.example.org"\n[output]\ntype="remote"\nbucket="example-org"\ndata_directory="data"\n[fetch]\nstart="2000-01-01"\n')
    policy = tmp_path / 'old-config' / 'fetch-config.toml'
    policy.parent.mkdir()
    policy.write_text('[wayback]\nworkers=1\nstarts_per_second=0.25\nretries=4\n[common-crawl]\nworkers=4\nstarts_per_second=8\nretries=4\n')
    return source, policy


def test_offline_migration_preserves_inputs_and_output(tmp_path):
    source, policy = legacy(tmp_path)
    original = policy.read_bytes()
    destination = tmp_path / 'new'
    migrate(source, destination, policy)
    assert not source.exists() and not policy.exists()
    assert (destination / 'fetch-config.toml').read_bytes() == original
    config = load_config(destination / 'collections' / 'example')
    assert config.presentation == {'id': 'example.org'}
    assert config.output.data_directory == destination / 'archives/example/data'
    assert (config.output.data_directory / 'payload').read_bytes() == b'preserve byte for byte'
    cached = next(config.index_directory.rglob('*.json'))
    validate_cache(cached, cached.relative_to(config.index_directory).as_posix())
    assert json.loads((destination / 'migration-inventory.json').read_text())['network_operations'] == 0


def test_migration_merges_full_metadata(tmp_path):
    source, policy = legacy(tmp_path)
    site = source / 'example'
    metadata = {'id': 'example.org', 'name': 'Example', 'homepage': 'https://example.org/', 'featured_capture': {'url': 'http://example.org/?', 'timestamp': '20040615000000'}, 'logo': {'src': 'assets/logo.gif', 'alt': 'Logo'}}
    (site / 'archive.json').write_text(json.dumps(metadata))
    (site / 'assets').mkdir()
    (site / 'assets/logo.gif').write_bytes(b'GIF89a')
    (site / 'navigator.toml').write_text('[source]\ntype="local"\n')
    migrate(source, tmp_path / 'new', policy)
    root = tmp_path / 'new/collections/example'
    assert load_config(root).presentation == metadata
    assert not (root / 'archive.json').exists()
    assert (root / 'assets/logo.gif').read_bytes() == b'GIF89a'
    assert 'type="local"' in (tmp_path / 'new/LOCAL-NOTES.md').read_text()


@pytest.mark.parametrize('failure', ['collision', 'unknown', 'invalid_cache', 'bad_metadata'])
def test_failed_migration_keeps_source(tmp_path, failure):
    source, policy = legacy(tmp_path)
    destination = tmp_path / 'new'
    if failure == 'collision':
        destination.mkdir()
    elif failure == 'unknown':
        (source / 'important.txt').write_text('keep')
    elif failure == 'invalid_cache':
        (source / 'example/index/2004.cdx.json').write_text('[null]')
    else:
        (source / 'example/archive.json').write_text('{"id":"different"}')
    with pytest.raises((ValueError, TypeError)):
        migrate(source, destination, policy)
    assert (source / 'example/fetch.toml').exists()
    assert policy.exists()
    if failure != 'collision':
        assert not destination.exists()
