# Workspace migration and bucket publication

## New local workspace

The [README diagrams](../README.md#user-workspace) define the user workspace and
bucket structure, including comments for every folder. Durable authored inputs
live under `~/archive-magic/collections/`; local output lives under `archives/`.

`collection.toml` replaces both `fetch.toml` and authored `archive.json`. Move
archive identity/presentation fields to `[collection]`, source/query/acquisition
fields to `[fetch]`, and destinations to `[storage.local]` and optional
`[storage.remote]`. `storage.local.directory` denotes the working root, not data/.
Relative paths resolve from collection.toml. Omit name/homepage for acquisition-only
collections; metadata publication requires both. Existing featured captures and
asset paths retain their values. Do not include credentials.

The old Fetch format is rejected; there is no legacy default-path fallback.
The shared fetch-config.toml moves from ~/.config/archive-magic-fetch/ to
~/archive-magic/. Explicit --config and ARCHIVE_MAGIC_FETCH_CONFIG still override it.

## Offline copy, verify, and remove

Stop Fetch writers and the local Navigator before migrating the workspace. From
the code checkout, the migration utility handles the former per-collection layout:

```sh
uv run python -m archive_magic_fetch.migrate_workspace \
  ./archives "$HOME/archive-magic" \
  --policy "$HOME/.config/archive-magic-fetch/fetch-config.toml"
```

This command performs no network operations. It refuses an existing destination,
symlinks, unfamiliar files, or a source that changes during migration. It obtains
the old archive locks, copies files to staging, compares hashes, validates converted
configurations/caches, then installs the destination and removes the old archive
tree and old shared policy file. Configuration values and asset/data/log bytes are
preserved. Bytecode and .DS_Store files are discarded. An inventory of original
and copied hashes is saved as migration-inventory.json.

The supported input layout has each fetch.toml beside data/, index/, logs/, and
optional assets/ and archive.json. Other layouts require an explicit manual
conversion; the utility refuses to guess. Legacy Wayback arrays are wrapped in
versioned envelopes and bound to the query in their adjacent fetch.toml. This is
an assumption inherited from the old cache contract, recorded in migration notes.
Common Crawl caches move into the v1 namespace without changing their envelopes.

The catalog, Navigator cache, local notes, and machine-specific launcher move too.
The launcher keeps its existing credential source. Obsolete navigator.toml contents
are recorded in LOCAL-NOTES.md, not adopted as current configuration. Empty local
data directories stay empty, with explicit restoration recorded as outstanding.
No bucket metadata is republished and no discovery caches are uploaded by migration.

## Publication, recovery, and freeing space

```sh
# Retry archive and discovery publication; no Wayback/Common Crawl acquisition.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --sync-only
# Restore managed WARC/CDXJ and discovery files without overwriting local conflicts.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --restore
# Verify actual bucket bytes, then remove local data, discovery, state, and logs.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --evict-local
# Validate and publish assets, then generate archive.json from collection.toml.
uv run archive-magic-fetch ~/archive-magic/collections/example.org --publish-metadata
```

A missing local baseline stops Fetch with restore instructions. Pending publications
must be finished before eviction. Local absence never requests remote deletion.
Ordinary publication preserves other years, unrelated objects, and obsolete remote
shards. Only explicit --reset-data removes managed remote archive data.

Metadata publication requires collection.id, name, and homepage. Optional fields
are description, logo, preview, and featured_capture. Images use contained assets/
paths, text alt descriptions, PNG/JPEG/WebP/GIF bytes, and at most 8 MiB each. The
generated manifest is limited to 1 MiB. Uploads place assets before archive.json;
data/discovery publication never implicitly publishes presentation edits.

A featured_capture table contains an HTTP(S) url and a quoted 14-digit UTC timestamp.
Navigator selects the nearest indexed capture, preferring the earlier one on ties.
Without a featured capture it selects the latest homepage capture. Preserve full
height preview images without replay toolbars; choose width for the archived site.
Navigator uses a top-aligned 16:10 crop, so authored assets can retain full height.

## Moving an existing flat bucket archive

Stop Fetch writers and plan a replay maintenance window. These instructions use
a dedicated `example-org` bucket; for a prefix, use its complete archive-root path.

1. List root-level objects and prepare a reviewed text file containing only the
   archive's WARC/CDXJ basenames, one per line. Exclude directories, assets, metadata,
   and other websites. Call it `/tmp/archive-files.txt` in the commands below.
2. Copy the selected files to `data/`; do not run sync or purge:

   ```sh
   rclone copy r2:example-org r2:example-org/data --files-from-raw /tmp/archive-files.txt
   ```

3. Verify the copies before deleting anything:

   ```sh
   rclone check r2:example-org r2:example-org/data --files-from-raw /tmp/archive-files.txt --one-way --download
   ```

   `--download` verifies content even when the provider lacks comparable hashes;
   this can transfer substantial data. Also compare the reviewed file list and
   byte sizes. Keep your existing local archive intact.
4. Prepare the catalog and collection.toml; publish metadata explicitly. Keep `storage.remote.prefix` at the archive
   root; the new implementation appends `data/` automatically. Replace old Navigator
   TOML configurations with the JSON catalog.
5. Explicitly remove only the reviewed old root objects after successful verification:

   ```sh
   rclone delete r2:example-org --files-from-raw /tmp/archive-files.txt --dry-run
   rclone delete r2:example-org --files-from-raw /tmp/archive-files.txt
   ```

6. Start the new Navigator, check each website, and resume Fetch publication.

Both applications reject remaining root-level `.warc.gz` or `.cdxj` objects as a
legacy layout, including when a cache exists. No migration happens automatically.
Use `--evict-local` to verify bucket content before freeing local space.
Use explicit `--restore` before resuming Fetch after eviction.

## Server configuration migration

`navigator.toml`, positional archive arguments, and directory catalogs have been
removed. Use `archive-magic-navigator --catalog /path/to/catalog.json`; one entry
serves one website. The new server only serves bucket-backed catalogs. Metadata
is stored in each bucket, never duplicated in the server catalog. Catalog order
is display order. All entries share endpoint, region, and compatible read-only
credentials. Editing this server file requires a restart.
