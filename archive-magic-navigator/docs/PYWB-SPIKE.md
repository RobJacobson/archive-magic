# pywb 2.9.1 Compatibility Spike

**Date:** July 28, 2026

**Result:** Passed, with one runtime packaging compatibility pin

## Environment

- macOS arm64
- CPython 3.12.13
- uv 0.11.31
- pywb 2.9.1
- setuptools 80.10.2

The isolated environment was created with:

```bash
uv venv --python 3.12 /tmp/archive-magic-navigator-spike/venv
uv pip install --python /tmp/archive-magic-navigator-spike/venv/bin/python \
  pywb==2.9.1
uv pip install --python /tmp/archive-magic-navigator-spike/venv/bin/python \
  'setuptools>=68,<81'
```

The published pywb 2.9.1 console entry point imports `pkg_resources`, but a
fresh uv environment does not install setuptools as a runtime dependency.
Without the compatibility pin, `wayback --version` fails with
`ModuleNotFoundError: No module named 'pkg_resources'`. Navigator therefore
declares `setuptools>=68,<81`; pywb itself remains pinned exactly to 2.9.1.

## Configurations

Single collection:

```yaml
enable_auto_colls: false
framed_replay: true
client_side_replay: false
collections:
  wecanstopthehate.org:
    index: /Users/rob/code/archive-magic/archives/wecanstopthehate.org/replay/index.cdxj
    archive_paths:
      - /Users/rob/code/archive-magic/archives/wecanstopthehate.org/
```

All collections:

```yaml
collections_root: /Users/rob/code/archive-magic/archives
index_paths: replay
archive_paths: .
framed_replay: true
client_side_replay: false
```

The server commands were:

```bash
wayback -d /tmp/archive-magic-navigator-spike/single -b 127.0.0.1 -p 18080
wayback -d /tmp/archive-magic-navigator-spike/all -b 127.0.0.1 -p 18081
```

## Results

- `/` and `/wecanstopthehate.org/` returned `200`.
- The URL query page and CDX endpoint exposed the collection's capture history.
- Response captures `20080201145454` and `20080303111916` returned different
  archived HTML bodies.
- Revisit capture `20110315163943` resolved digest
  `sha1:TM256PD5SIRSJ6UFHDECWQRKOMJPVKY5` to the full response payload from
  `20110306160913`.
- `/styles/additional.css` returned archived CSS.
- Rewritten HTML changed stylesheet references to timestamped pywb `cs_` URLs.
- Framed replay rendered pywb's outer frame and `ContentFrame`.
- A deliberately absent PNG returned `404`; no live collection or fallback
  was configured.
- Automatic discovery listed and replayed `wecanstopthehate.org`.
- Direct collection-root `archive_paths` correctly resolved indexed
  `archive/...` filenames.
- A browser smoke test followed the branded collection form into pywb's
  calendar, which displayed 193 captures across 2008–2016, then opened framed
  capture `20080201145454` with the pywb navigation controls and archived page
  visible together.

Pywb emitted deprecation and Python escape-sequence warnings on initial import.
Sending Ctrl-C directly to the unwrapped pywb command also printed a
`KeyboardInterrupt` traceback. Navigator avoids the latter by owning the child
session and terminating it after the parent handles Ctrl-C.

Pywb 2.9.1's root-page Jinja loader searches `templates/` beneath the server
working directory before its packaged defaults; an absolute `templates_dir`
does not affect the root page. Navigator therefore copies its two original
packaged overrides and stylesheet into the ephemeral runtime directory before
startup. No template or static file is written beneath a collection.

## Read-only proof

Before and after both server modes, the metadata inventory digest was:

```text
5596602065ed569204ce63a332dee64a4ca87d977b4bf0ca134fb7e079cf068b
```

The aggregate file-content digest was:

```text
3d8e7694674cf086e48744722762e2376fae0d98e2b29ea14547986ffea1ea9f
```

No path, type, size, modification time, or file content beneath the collection
changed.

## Wrapper configuration decision

The automatic all-collection configuration above records what the spike
successfully tested. Navigator itself uses explicit `collections` entries and
sets `enable_auto_colls: false` in both CLI modes. This keeps single- and
multi-collection configuration identical and ensures pywb exposes only the
collections that Navigator validated before startup. Pywb still opens the
configured CDXJ and WARC paths on demand, so replacing data at an existing path
does not require a private Navigator copy.
