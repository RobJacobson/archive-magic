# Archive Magic Navigator

A standalone catalog and replay server for private S3-compatible archive buckets.
It needs no Fetch installation, configuration, or local WARC files.

```console
uv sync
uv run archive-magic-navigator --catalog /path/to/catalog.json --open
```

The only persisted Navigator configuration is [catalog.json](../examples/catalog.json).
Its ordered entries specify bucket/prefix locations with a shared storage endpoint
and region. Credentials come from the standard AWS chain; `.env` is not loaded.
Each bucket supplies [archive.json](../examples/example.org/archive.json), optional
`assets/`, and WARC/CDXJ files under `data/`. Both JSON formats are unversioned.

The homepage displays organization metadata, logo, preview, and indexed capture
coverage. It links to an archived entry page and to capture search. PNG, JPEG,
WebP, and GIF assets remain private in storage and are served from Navigator's
cache. Missing images have placeholders.

Runtime options: `--bind` (localhost), `--port` (8080), `--cache`,
`--poll-interval` (300 seconds), `--wayback-fallback {on,off}` (on), `--open`,
and `--debug`. Cache defaults to `navigator-cache/` beside the catalog and contains
only manifests, assets, and indexes. WARC payloads are read by authenticated range
requests. Configuration changes require restart; content refreshes automatically.

Invalid catalog JSON and startup duplicate IDs fail startup. Individual bucket
failures leave unavailable cards while other archives serve. Failed refreshes
retain accepted snapshots; an index cache cannot preserve inaccessible WARC bytes.
Recovered entries activate without a server restart. Changes to an accepted ID
require a restart.

`navigator.toml`, directory catalogs, positional archive configuration, and the
local-source CLI are removed. Follow the [migration guide](../docs/BUCKET-CATALOG-MIGRATION.md)
for configuration and old flat bucket layouts.

This remains an unauthenticated development replay server without TLS or hostile
content hardening. See [architecture](docs/ARCHITECTURE-NAVIGATOR.md).

```console
uv run pytest -q -m 'not integration'
uv run pytest -q -m integration
```

Integration tests require local socket binding and use simulated private buckets;
they do not require cloud credentials or change real buckets.
