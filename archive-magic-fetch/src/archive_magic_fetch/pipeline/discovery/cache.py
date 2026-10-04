"""Portable discovery cache provenance and validation."""
import hashlib
import json
from pathlib import Path

from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search


def query_for(pattern):
    url, match = normalize_cdx_search(pattern)
    return {'url': url, **({'matchType': match} if match is not None else {})}


def query_hash(query):
    return hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()


def wayback_path(root, pattern, year):
    return Path(root) / 'wayback' / 'v1' / query_hash(query_for(pattern)) / f'{year:04d}.cdx.json'


def wayback_document(pattern, year, captures):
    return {'version': 1, 'source': 'wayback', 'query': query_for(pattern), 'year': year, 'captures': captures}


def validate_wayback(value, *, query=None, year=None):
    from .load_or_fetch_year_cdx import _capture_from_dict
    if not isinstance(value, dict) or set(value) != {'version', 'source', 'query', 'year', 'captures'}:
        raise ValueError('invalid Wayback cache envelope; migrate legacy discovery caches')
    if value['version'] != 1 or value['source'] != 'wayback' or not isinstance(value['query'], dict) or not isinstance(value['captures'], list):
        raise ValueError('invalid Wayback discovery metadata')
    query_value = value['query']
    if (set(query_value) - {'url', 'matchType'} or not isinstance(query_value.get('url'), str)
            or not query_value['url'] or ('matchType' in query_value and query_value['matchType'] not in {'exact', 'prefix', 'host', 'domain'})):
        raise ValueError('invalid Wayback discovery query')
    actual_year = value['year']
    if type(actual_year) is not int or not 1 <= actual_year <= 9999:
        raise ValueError('invalid Wayback discovery year')
    if query is not None and value['query'] != query or year is not None and actual_year != year:
        raise ValueError('Wayback cache query/year mismatch')
    captures = tuple(_capture_from_dict(item) for item in value['captures'])
    if any(int(item.identity.timestamp[:4]) != actual_year for item in captures):
        raise ValueError('Wayback cache captures outside year')
    return captures


def validate_cache(path, relative):
    value = json.loads(path.read_text())
    parts = relative.split('/')
    if parts[:2] == ['wayback', 'v1'] and len(parts) == 4:
        validate_wayback(value)
        expected = f"wayback/v1/{query_hash(value['query'])}/{value['year']:04d}.cdx.json"
    elif parts[:2] == ['common-crawl', 'v1'] and len(parts) == 5:
        from .load_or_fetch_common_crawl_year import _load_cache
        metadata = {key: value[key] for key in ('version', 'query', 'from', 'to', 'collection')}
        if metadata['version'] != 1 or _load_cache(path, metadata) is None:
            raise ValueError('invalid Common Crawl discovery cache')
        year = metadata['from'][:4]
        if metadata['from'] != year + '0101000000' or metadata['to'] != year + '1231235959':
            raise ValueError('discovery cache must cover a full calendar year')
        expected = f"common-crawl/v1/{query_hash(value['query'])}/{value['collection']['id']}/{year}.json"
    else:
        raise ValueError('unknown discovery cache namespace')
    if relative != expected:
        raise ValueError('discovery path/provenance mismatch')
