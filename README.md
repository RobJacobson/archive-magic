# Archive Magic

Fetch downloads Wayback or Common Crawl captures into WARC files and replay CDXJ
indexes. Navigator independently serves published collections from private
S3-compatible buckets, including Cloudflare R2.

Install the code workspace with `uv sync`. Fetch uses the AWS credential chain
for S3 operations; its explicit destructive remote reset additionally requires
rclone. Neither component automatically loads `.env` files.

## User workspace

Keep working files in `~/archive-magic`, separate from this code checkout:

```text
~/archive-magic/                       # User workspace; survives replacing the code
├── fetch-config.toml                 # Shared per-source pacing and retry settings
├── catalog.json                      # Navigator's ordered bucket catalog
├── LOCAL-NOTES.md                    # Optional local operational notes
├── run-navigator.py                  # Optional machine-specific credential launcher
├── collections/                      # Durable, authored collection definitions
│   └── example.org/                  # One independently managed collection
│       ├── collection.toml           # Acquisition, storage, and presentation settings
│       ├── assets/                   # Original logos and preview images
│       └── .archive-magic.lock       # Runtime lock; survives archive eviction
├── archives/                         # Local output; restorable after S3 publication
│   └── example.org/                  # Safe to evict after remote verification
│       ├── data/                     # WARC payloads and replay CDXJ indexes
│       ├── discovery/                # Completed source discovery caches
│       ├── logs/                     # Disposable diagnostics; never uploaded
│       └── .state/                   # Publication/recovery receipts; not logs
└── cache/                            # Rebuildable application caches
    └── navigator/                    # Bucket metadata, images, and replay indexes
```

The bucket or collection prefix contains:

```text
<bucket>/<optional-prefix>/           # Published collection root
├── archive.json                      # Generated presentation manifest for Navigator
├── assets/                           # Explicitly published presentation images
├── data/                             # WARCs and replay CDXJ indexes
└── discovery/                        # Validated discovery caches for future Fetch runs
```

Local logs, receipts, locks, credentials, and `collection.toml` are never uploaded.
Back up `collections/`, shared configuration, and operational notes separately.
S3 publication does not back up your authored acquisition settings.

## Collection configuration

Copy [collection.toml](examples/collections/example.org/collection.toml) to
`~/archive-magic/collections/<name>/collection.toml`. This is the only authored
configuration for that collection. The collection ID is shared by acquisition,
filenames, and presentation metadata.

```toml
[collection]
id = "example.org"
name = "Example Organization"
homepage = "https://example.org/"

[collection.logo]
src = "assets/logo.png"
alt = "Example Organization"

[fetch]
source = "wayback"
url_pattern = "*.example.org"
start = "2000-01-01"

[storage.local]
directory = "../../archives/example.org"

[storage.remote]
bucket = "example-org"
prefix = ""
endpoint_url = "https://ACCOUNT_ID.r2.cloudflarestorage.com"
region = "auto"
```

Omit `[storage.remote]` for local-only acquisition. For AWS S3, omit
`endpoint_url` and use the bucket's region. The local directory resolves relative
to `collection.toml`; it must not overlap collection definitions or assets.
Fetch derives `data/`, `discovery/`, `logs/`, and `.state/` underneath it.

`name` and `homepage` may be omitted for acquisition-only collections; metadata
publication requires them. Optional presentation fields are `description`,
`logo`, `preview`, and `featured_capture` (HTTP(S) `url`, quoted 14-digit UTC
`timestamp`). Images use contained `assets/` paths and text `alt` descriptions.

The old `fetch.toml` format is rejected. See the
[migration and publication guide](docs/BUCKET-CATALOG-MIGRATION.md).

## Fetch and storage lifecycle

Run these commands from the code checkout, or use the installed entry points:

```sh
# Acquire captures; automatically publish completed data and discovery to S3.
uv run archive-magic-fetch ~/archive-magic/collections/example.org

# Retry data/discovery publication without contacting upstream capture sources.
# Works for discovery-only output after WARC acquisition failed.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --sync-only

# Explicitly recover data and discovery caches from the bucket.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --restore

# Verify remote copies by content, then remove this collection's local output.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --evict-local

# Publish authored assets, then generate and publish archive.json.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --publish-metadata
```

Operation flags are mutually exclusive. Restore, eviction, and metadata
publication require remote storage and cannot use acquisition date/reset flags.
Restoring does not overwrite differing local files: preserve or resolve those
files first. Interrupted restore and publication operations can be retried.

**Local deletion never requests remote deletion.** Fetch requires explicit
`--restore` when a bucket-backed collection is missing local published content.
Publication checks the remote baseline and records pending writes in `.state/`;
conflicting remote or unrecorded local changes stop publication. Keep one active
writer per bucket/prefix. Collection locks protect processes using the same
local definition; there is no distributed lock between machines.

WARCs upload before replay indexes. Ordinary publication does not prune obsolete
remote shards or delete missing years. Explicit `--reset-data` remains destructive:
remote mode rejects date overrides, clears managed remote WARC/CDXJ files under
`data/`, and rebuilds the full configured range. It preserves discovery caches,
metadata, assets, and unrelated objects; replay can be unavailable during reset.

`--evict-local` refuses unpublished, changed, unknown, or unfinished output. It
checks actual remote content, including multipart objects whose ETags are not
content hashes. Verification can download as much data as the local archive.
Navigator keeps serving from S3 after eviction. Manual deletion is also possible,
but stop Fetch first and verify that needed data and discovery are published.
Local-only collections have no bucket copy and cannot use restore or eviction.

## Shared Fetch policy and discovery

Shared settings live at `~/archive-magic/fetch-config.toml`; see the
[example](examples/fetch-config.toml). A missing selected file is created with
four workers, eight request starts per second, and four retries for each source.
`--config PATH` overrides `ARCHIVE_MAGIC_FETCH_CONFIG`, which overrides the default.
`XDG_CONFIG_HOME` no longer selects this file. Existing settings are not overwritten.
`--workers`, `--starts-per-second`, and `--retries` override policy for one run.

Historical Wayback queries cover complete calendar years and persist under
`discovery/wayback/v1/<query-hash>/YYYY.cdx.json`, with query provenance and
validated capture records. The current UTC year is queried afresh and never
persisted; future years are skipped. Date restrictions filter playback, not discovery.

Common Crawl caches complete per-crawl calendar-year queries under
`discovery/common-crawl/v1/<query-hash>/<crawl-id>/YYYY.json`. Each run refreshes
the crawl catalog; unchanged complete caches are reused, including current-year
crawl results. Query changes select a different namespace. Source revisits,
legacy ARC, and malformed records requiring historical repair remain unsupported.

Completed caches upload before subsequent WARC work, even if that work fails.
Only complete caches are published. Invalid caches fail rather than being silently
replaced. Restore explicitly recovers caches; ordinary Fetch does not silently
restore absent remote files. Source/query changes do not erase existing WARCs;
use a separate collection definition/output directory for isolated acquisitions.

Logs are disposable and local. `--trace-requests` adds request CSV diagnostics;
429 responses also produce bounded diagnostic excerpts. Pacing is per process,
with a separate minimum 2.5-second Wayback CDX spacing. See
[Fetch architecture](archive-magic-fetch/docs/ARCHITECTURE-FETCH.md) for details.

## Navigator

Copy [catalog.json](examples/catalog.json) to `~/archive-magic/catalog.json` and
configure the endpoint, region, and ordered bucket/prefix entries. Use credentials
with read/list access outside JSON. Navigator requires no Fetch installation,
collection definitions, discovery caches, or local WARC files.

```sh
uv run archive-magic-navigator --open
# Override deployment configuration and cache when needed:
uv run archive-magic-navigator --catalog /path/to/catalog.json --cache /path/to/cache
```

The default catalog is `~/archive-magic/catalog.json`; the default cache is
`cache/navigator/` beside the selected catalog. Navigator downloads presentation
metadata, images, and replay indexes, then streams WARC ranges from the bucket.
It ignores discovery caches and works with the entire local `archives/` tree absent.

Metadata is generated by Fetch's explicit `--publish-metadata` command; editing
`collection.toml` does not change the served site until publication. Assets publish
before the manifest. Navigator refreshes content periodically; catalog or identity
changes require a restart. `--poll-interval` defaults to 300 seconds.

Other flags include `--bind`, `--port`, `--wayback-fallback {on,off}` (default on),
`--open`, and `--debug`. It defaults to localhost and remains an unauthenticated
development replay server without production TLS or hostile-content isolation.
See [Navigator architecture](archive-magic-navigator/docs/ARCHITECTURE-NAVIGATOR.md).
