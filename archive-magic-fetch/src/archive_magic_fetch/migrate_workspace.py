"""One-time, offline migration of a legacy local workspace.

Copy and validate every durable input/output before removing the source tree.
No network clients are constructed. Legacy Wayback provenance is inferred from
its adjacent fetch.toml, matching the old cache contract.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import tomllib
from contextlib import ExitStack

from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.config.load_archive_config import load_config
from archive_magic_fetch.pipeline.discovery.cache import wayback_path, wayback_document, validate_cache
from archive_magic_fetch.runtime.manage_archive_files import archive_lock


def toml_document(document):
    """Serialize the scalar/nested-table types used by collection configurations."""
    lines = []
    def table(value, prefix=''):
        if prefix:
            lines.append(f'[{prefix}]')
        for key, item in value.items():
            if not isinstance(item, dict):
                lines.append(f'{key} = {json.dumps(item, ensure_ascii=False)}')
        lines.append('')
        for key, item in value.items():
            if isinstance(item, dict):
                table(item, f'{prefix}.{key}' if prefix else key)
    table(document)
    return '\n'.join(lines)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def migrate(source: Path, destination: Path, policy: Path):
    source, destination, policy = source.resolve(), destination.expanduser().resolve(), policy.expanduser().resolve()
    if destination.exists():
        raise ValueError(f'destination already exists: {destination}')
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('migration source and destination must be separate')
    if not source.is_dir() or not policy.is_file():
        raise ValueError('existing archives directory and fetch-config.toml are required')
    recipes = sorted(source.glob('*/fetch.toml'))
    if not recipes:
        raise ValueError('no legacy fetch.toml collections found')
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'migration requires regular files/directories: {path}')
    ignored = lambda p: p.name in {'.DS_Store', '.archive-magic.lock'} or '__pycache__' in p.parts
    with ExitStack() as locks:
        for recipe in recipes:
            old = tomllib.loads(recipe.read_text())
            data = (recipe.parent / old['output'].get('data_directory', 'data')).resolve()
            if data != recipe.parent / 'data':
                raise ValueError('migration supports the existing per-collection data/ layout only')
            locks.enter_context(archive_lock(ArchiveLayout(data, old['archive']['id'])))
        original = {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file() and not ignored(p)}
        policy_hash = digest(policy)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.archive-magic-migration-', dir=destination.parent) as temp:
            staged = Path(temp) / 'workspace'
            staged.mkdir()
            accounted = set()
            copied = {}
            records = []
            def copy(src, dst):
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                if digest(src) != digest(dst):
                    raise ValueError(f'copy verification failed: {src}')
                if src.is_relative_to(source):
                    accounted.add(str(src.relative_to(source)))
                copied[str(dst.relative_to(staged))] = digest(dst)
            for recipe in recipes:
                name = recipe.parent.name
                old = tomllib.loads(recipe.read_text())
                presentation = {'id': old['archive']['id']}
                manifest = recipe.parent / 'archive.json'
                if manifest.exists():
                    presentation = json.loads(manifest.read_text())
                    if presentation['id'] != old['archive']['id']:
                        raise ValueError('collection/manifest ID mismatch')
                    accounted.add(str(manifest.relative_to(source)))
                output = dict(old['output'])
                mode = output.pop('type')
                output.pop('data_directory', None)
                fetch = {key: value for key, value in old['archive'].items() if key != 'id'} | old.get('fetch', {})
                document = {'collection': presentation, 'fetch': fetch, 'storage': {'local': {'directory': f'../../archives/{name}'}}}
                if mode == 'remote':
                    document['storage']['remote'] = output
                elif mode != 'local' or output:
                    raise ValueError('unsupported legacy output settings')
                definition = staged / 'collections' / name
                definition.mkdir(parents=True)
                (definition / 'collection.toml').write_text('# Authored inputs; local archives can be removed independently.\n' + toml_document(document))
                accounted.add(str(recipe.relative_to(source)))
                working = staged / 'archives' / name
                for directory in ('data', 'discovery', 'logs'):
                    (working / directory).mkdir(parents=True)
                for category in ('assets', 'data', 'logs'):
                    base = recipe.parent / category
                    for src in sorted(base.rglob('*')) if base.exists() else []:
                        if src.is_file() and not ignored(src):
                            target = definition if category == 'assets' else working
                            copy(src, target / category / src.relative_to(base))
                discovery = recipe.parent / 'index'
                for src in sorted(discovery.rglob('*')) if discovery.exists() else []:
                    if not src.is_file() or ignored(src):
                        continue
                    relative = src.relative_to(discovery)
                    if len(relative.parts) == 1 and src.name.endswith('.cdx.json'):
                        year = int(src.name[:4])
                        dst = wayback_path(working / 'discovery', fetch['url_pattern'], year)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_text(json.dumps(wayback_document(fetch['url_pattern'], year, json.loads(src.read_text()))) + '\n')
                        accounted.add(str(src.relative_to(source)))
                    elif relative.parts[0] == 'common-crawl':
                        dst = working / 'discovery' / 'common-crawl' / 'v1' / Path(*relative.parts[1:])
                        copy(src, dst)
                    else:
                        raise ValueError(f'unrecognized discovery file; preserve and review: {src}')
                    validate_cache(dst, dst.relative_to(working / 'discovery').as_posix())
                legacy = recipe.parent / 'navigator.toml'
                if legacy.exists():
                    records.append(f'Obsolete {name}/navigator.toml (not used by current Navigator):\n```toml\n{legacy.read_text()}\n```')
                    accounted.add(str(legacy.relative_to(source)))
                config = load_config(definition)
                if config.archive_id != old['archive']['id'] or config.url_pattern != old['archive']['url_pattern']:
                    raise ValueError('converted configuration verification failed')
                if not list((working / 'data').iterdir()) and mode == 'remote':
                    records.append(f'{name}: local data is empty; explicit --restore remains outstanding. No bucket was accessed.')
            for name in ('catalog.json', 'run-navigator.py'):
                if (source / name).exists():
                    copy(source / name, staged / name)
            cache = source / 'navigator-cache'
            for src in sorted(cache.rglob('*')) if cache.exists() else []:
                if src.is_file() and not ignored(src):
                    copy(src, staged / 'cache' / 'navigator' / src.relative_to(cache))
            notes = source / 'LOCAL-NOTES.md'
            text = notes.read_text() if notes.exists() else ''
            if notes.exists():
                accounted.add(str(notes.relative_to(source)))
            for recipe in recipes:
                name = recipe.parent.name
                text = text.replace(str(recipe.parent), str(destination / 'collections' / name))
                text = text.replace(f'{name}/fetch.toml', f'collections/{name}/collection.toml')
                text = text.replace(f'{name}/archive.json', f'collections/{name}/collection.toml')
            text = text.replace(str(source), str(destination)).replace('uv run python archives/run-navigator.py', f'uv run python {destination}/run-navigator.py')
            text = text.replace("This file is local-only: the repository's `archives/` Git ignore rule excludes\nit.", "This file is local-only, outside the code repository.")
            text = text.replace('`fetch.toml` intentionally', '`collection.toml` intentionally')
            text = text.replace('Git-ignored local launcher', 'local workspace launcher')
            text = text.replace('- Archive root:', '- Collection definition root:')
            text = text.replace('ordinary Fetch synchronization only manages archive data.', 'ordinary Fetch publication manages archive data and discovery caches.')
            text = text.replace('Only `wecanstopthehate.org` has been migrated in this work.', 'The earlier bucket-layout migration covered only `wecanstopthehate.org`.')
            text += '\n\n## Workspace migration\n\nLegacy per-collection Wayback caches were bound to the query in their adjacent fetch.toml during conversion.\n\n'
            text += '\n\n'.join(records) + '\n\nDefinitions and assets are in collections/. Generated data, discovery caches, and disposable logs are in archives/. Navigator cache is in cache/navigator/. No remote changes were made.\n'
            (staged / 'LOCAL-NOTES.md').write_text(text)
            copy(policy, staged / 'fetch-config.toml')
            if accounted != set(original):
                raise ValueError(f'unaccounted source files: {sorted(set(original) - accounted)}')
            if original != {str(p.relative_to(source)): digest(p) for p in source.rglob('*') if p.is_file() and not ignored(p)} or digest(policy) != policy_hash:
                raise ValueError('source changed during migration')
            report = {'source': str(source), 'original_sha256': original, 'copied_sha256': copied, 'policy_sha256': policy_hash, 'network_operations': 0}
            (staged / 'migration-inventory.json').write_text(json.dumps(report, indent=2) + '\n')
            os.rename(staged, destination)
        # Definitions were validated while staged; relative paths remain identical.
        for recipe in recipes:
            load_config(destination / 'collections' / recipe.parent.name)
        shutil.rmtree(source)
        policy.unlink()
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--policy', type=Path, required=True)
    args = parser.parse_args()
    print(migrate(args.source, args.destination, args.policy))


if __name__ == '__main__':
    main()
