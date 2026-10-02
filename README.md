# Archive Magic

Archive Magic Fetch downloads Internet Archive captures into WARC files and CDXJ
indexes. Archive Magic Navigator independently serves published archives from
private S3-compatible buckets, including Cloudflare R2.

Install the workspace with `uv sync`. Fetch also requires rclone.

## Archive layout

On the Fetch host:

```text
example.org/
  fetch.toml
  archive.json
  assets/
    logo.png
    preview.jpg
  data/
    example.org-2004-001.warc.gz
    example.org-2004-index.cdxj
  logs/
```

The bucket (or configured archive-root prefix) contains only `archive.json`,
`assets/`, and `data/`. Configuration files and logs remain local. Logs retain
their existing location beside the local data directory.

The local `archive.json` and images are authoring copies. Explicitly publish them
when ready; Navigator reads their bucket copies. Ordinary Fetch publication
neither uploads nor deletes presentation metadata and images.

## Fetch

Copy [fetch.toml](examples/example.org/fetch.toml) to the website root and set
its archive ID, URL pattern, dates, and storage destination. `output.prefix`
identifies the archive root; Fetch appends `data/` automatically. Relative local
paths resolve from `fetch.toml`, with `data_directory = "data"` by default.
Use `output.type = "local"` for acquisition without remote publication.

```console
uv run archive-magic-fetch /path/to/example.org
uv run archive-magic-fetch /path/to/example.org --sync-only
```

Fetch defaults to 4 workers, 16 starts/second, and 4 retries. Date flags may narrow
the configured range. Each year is staged and validated locally before publication:
WARCs are copied first, CDXJ indexes synced second, and obsolete WARCs removed last.
After an upload failure, `--sync-only` retries without contacting Wayback.

Local `data/` remains authoritative for Fetch's WARC/CDXJ mirror. Do not delete
local archive files to free space and then sync: missing managed local files can
be deleted remotely. Fetch does not restore or evict local history automatically.

Remote `--reset-data` rejects date overrides and rebuilds the entire configured
range. It deletes only this archive's managed WARC/CDXJ objects under remote
`data/`, preserving metadata, images, and unrelated objects. It clears the local
working data directory. Replay is unavailable until new indexes are published.
Logs remain local and unchanged in location.

## Navigator

Copy [catalog.json](examples/catalog.json) to the Navigator server. List the
bucket and optional prefix of each website in display order. No Fetch files are
needed on that server, and no `navigator.toml` is used. Use a one-entry catalog
for a single website.

```console
uv run archive-magic-navigator --catalog /path/to/catalog.json --open
```

Configure the shared endpoint and region in the catalog and use standard AWS
credentials outside JSON. Navigator needs read/list access to the selected
buckets. For AWS S3, omit `endpoint_url` and set the appropriate region. Both
programs use the standard credential environment/profile and do not load `.env`.

Each bucket's [archive.json](examples/example.org/archive.json) supplies its ID,
name, homepage, optional description, logo, preview, and optional featured capture.
The ID matches WARC/CDXJ filenames. Images use bucket-relative `src` and `alt`;
external URLs are not accepted. Neither JSON format has a schema-version field.

Navigator downloads indexes and presentation assets, then streams WARC byte
ranges directly from storage. Its cache defaults to `navigator-cache/` beside
the catalog. Runtime flags include `--cache`, `--poll-interval` (default 300
seconds), `--bind`, `--port`, `--wayback-fallback {on,off}` (default on), `--open`,
and `--debug`.

The homepage displays metadata and actual capture coverage. A primary link opens
the nearest featured capture or latest homepage capture; if absent, it opens
archive search. Missing images use placeholders. Failed buckets do not prevent
healthy archives from serving. Metadata, images, and indexes refresh periodically;
server catalog edits and archive identity changes require a restart.

Navigator defaults to localhost. It remains an unauthenticated development replay
server, without production TLS or hostile-content isolation.

## Publishing metadata and migrating

See [the migration and publication guide](docs/BUCKET-CATALOG-MIGRATION.md) for
manual metadata uploads and moving existing flat bucket archives into `data/`.
Fetch and Navigator detect old root-level WARC/CDXJ files and require migration;
they do not move or delete them automatically.

See the [Fetch architecture](archive-magic-fetch/docs/ARCHITECTURE-FETCH.md) and
[Navigator architecture](archive-magic-navigator/docs/ARCHITECTURE-NAVIGATOR.md)
for implementation details.
