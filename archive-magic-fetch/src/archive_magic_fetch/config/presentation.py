"""Validate authored presentation metadata without depending on Navigator."""
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import re


def web_url(value):
    if not isinstance(value, str):
        raise ValueError('URL must be text')
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in value):
        raise ValueError('URL must be HTTP(S), without credentials')
    return value


def metadata(value, *, required=False):
    allowed = {'id', 'name', 'homepage', 'description', 'logo', 'preview', 'featured_capture'}
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError('unknown collection presentation fields')
    for key in ('id', 'name', 'description'):
        if key in value and (not isinstance(value[key], str) or any(ord(c) < 32 for c in value[key]) or (key != 'description' and not value[key].strip())):
            raise ValueError(f'collection.{key} must be text')
    if required and not {'id', 'name', 'homepage'} <= value.keys():
        raise ValueError('metadata publication requires collection.id, name, and homepage')
    if 'homepage' in value:
        web_url(value['homepage'])
    for role in ('logo', 'preview'):
        if role not in value:
            continue
        image = value[role]
        if not isinstance(image, dict) or set(image) != {'src', 'alt'}:
            raise ValueError(f'{role} requires src and alt')
        src = image['src']
        if not isinstance(src, str) or not src.startswith('assets/') or any(p in {'', '.', '..'} for p in src.split('/')) or any(c in src for c in '\\:%?#') or any(ord(c) < 32 for c in src):
            raise ValueError('image src must be a contained assets/ path')
        if not isinstance(image['alt'], str) or any(ord(c) < 32 for c in image['alt']):
            raise ValueError('image alt must be text')
    if 'featured_capture' in value:
        featured = value['featured_capture']
        if not isinstance(featured, dict) or set(featured) != {'url', 'timestamp'}:
            raise ValueError('featured_capture requires url and timestamp')
        web_url(featured['url'])
        stamp = featured['timestamp']
        if not isinstance(stamp, str) or not re.fullmatch(r'\d{14}', stamp):
            raise ValueError('featured timestamp must contain 14 digits')
        datetime.strptime(stamp, '%Y%m%d%H%M%S')
    return dict(value)


def asset_path(root: Path, src: str) -> Path:
    path = (root / src).resolve()
    if not path.is_relative_to(root.resolve() / 'assets'):
        raise ValueError('asset escapes collection assets directory')
    return path
