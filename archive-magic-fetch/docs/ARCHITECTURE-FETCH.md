# Archive Magic Fetch Architecture

## Purpose and boundary

Archive Magic Fetch turns Internet Archive capture history into portable WARC 1.1
collections and CDXJ indexes. It is a standalone producer. It does not call or
coordinate with Navigator; the two applications interact only by reading and
writing the locations described by the same archive descriptor.

One Fetch process on one machine owns an archive prefix. Concurrent writers,
secondary updater machines, and manual bucket mutation are unsupported.

## Public interface

```text
archive-magic-fetch ARCHIVE [--start DATE] [--end DATE] [--reset-data]
```

`ARCHIVE` is either a file named `archive.toml` or its containing directory. There
is no legacy URL-pattern, `--config`, or `--archives-root` interface.

Normal CLI dates narrow a run without changing the descriptor. Without overrides,
Fetch uses `[fetch].start` through `[fetch].end`; an omitted end is resolved to the
current UTC timestamp when the run starts. Fetch therefore checks the complete
configured history on daily or weekly runs. Identity-based deduplication makes
unchanged older collections no-ops, so normally only the current year is
republished.

## Descriptor contract

The descriptor is user-authored intent:

```toml
schema_version = 1

[archive]
id = "example.org"
url_pattern = "*.example.org"

[storage]
authority = "remote" # "local" or "remote"
workspace_directory = "workspace"

[storage.remote]
bucket = "archive-magic"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"

[fetch]
start = "1995-01-01"
# end omitted means now
warc_target_bytes = 250000000
playback_workers = 4
playback_starts_per_second = 20.0
retries = 4 # four retries after the initial request

[playback]
wayback_fallback = true
```

Rules:

- `schema_version` must be exactly `1`; unknown keys are errors.
- Archive IDs must be route-safe and cannot be `.`, `..`, or `static`.
- Relative paths and `~` are resolved from the descriptor directory.
- `workspace_directory` is the exact archive root. The archive ID is not appended.
- `storage.remote` is required for remote authority and forbidden for local
  authority.
- Bucket credentials come exclusively from Boto3's standard credential chain.
  No adjacent `.env` is loaded and no specific access-key variable is required.
- The compressed WARC rollover target defaults to 250,000,000 bytes and remains
  configurable per archive.
- `[fetch].retries` applies to both CDX and playback requests and defaults to four
  retries after the initial request. CDX retries are owned by Fetch: HTTP 429 and
  TCP connection refused pause for 60 seconds (or `Retry-After`) before the next
  attempt, matching playback backpressure. A CDX failure skips that year, continues
  with later years, and makes the process exit nonzero.

Fetch validates the complete shared descriptor, including playback keys, so Fetch
and Navigator reject the same malformed contract.

## Workspace and publication layout

```text
<workspace_directory>/
  collections-manifest.json
  collections/
    2004/
      example.org-2004-001.warc.gz
      example.org-2004-002.warc.gz
      example.org-2004-index.cdxj
  captures/
    2004/
      runs/<run-id>/run.json
```

`collections/` is portable playback data. `captures/` records local diagnostics,
queries, and run state; it is deliberately not bucket-authoritative archive
content. `collections-manifest.json` is generated publication state and retains its
existing unversioned JSON shape. It must never be edited as configuration.

CDXJ and manifest files use same-directory temporary names followed by atomic local
replacement. WARC records are validated as independent gzip members before being
appended to a shard.

## Acquisition pipeline

For each year in the selected range, Fetch:

1. Ensures the year's CDXJ. Local authority is a no-op; remote authority
   downloads the committed CDXJ only if no local copy exists. Leftover local
   WARCs from an interrupted run are reindexed before inventory.
2. Queries Internet Archive CDX history through `WaybackClient.search()`, which
   owns parsing and resume-key pagination. Fetch owns CDX retries and treats
   connection refused like HTTP 429: pause 60s (or `Retry-After`) and retry the
   whole year query. Giving up on a year does not abort later years.
3. Parses and deduplicates captures by canonical capture identity.
4. Inventories existing captures from CDXJ identity metadata and skips those
   already represented.
5. If that year has captures not yet represented, remote authority downloads
   only the manifest's final WARC for the year (or keeps a longer local tail).
   Unchanged years never download a WARC.
6. Resolves remaining captures through bounded, rate-limited playback workers.
7. Appends response or revisit records through one serialized WARC writer.
8. Reindexes changed/new WARCs, merges their lines into the stable CDXJ, and
   validates the completed artifacts.
9. Publishes only changed/new WARCs plus the CDXJ, writes an immutable local run
   record, and evicts remote working copies after confirmed success.

A weekly run and a multi-year backfill are the same loop. New captures in the
current year extend that year's last shard. Missed captures from an earlier year
extend that earlier year's last shard. Earlier shards of a year are never
downloaded or rewritten.

Acquisition can be parallel; WARC mutation and publication remain serialized.
Failures for individual captures are recorded without corrupting already committed
records. A year-level failure (including CDX) skips that year, continues with the
rest of the range, and produces a nonzero exit. Uncaught exceptions at the process
boundary also produce a nonzero exit. The next run keeps any leftover local
tail/CDXJ, rebuilds the index if needed, and republishes. `--reset-data` is the
recovery tool when the workspace was lost or the prefix is confused.

Payloads are never reused across collections: a capture missing from its current
collection is downloaded from Internet Archive unless CDX metadata can synthesize
an empty response or slash redirect. The `payload-reuses` metric counts only those
CDX-synthesized outcomes.

## Append-only WARC behavior

WARC files use gzip member concatenation. Existing records are immutable. The
writer extends the last shard of a year, or creates the next sequence when that
shard is at or beyond the target. A normal update therefore uploads one tail WARC
plus its CDXJ, or one new WARC plus the CDXJ. Earlier WARC objects remain
untouched.

Each response or revisit is first serialized and digest-validated in memory as a
complete gzip member. Only those validated bytes are appended. If the filesystem
reports an append failure, the writer truncates back to the prior byte length.
Closing a changed shard validates the complete WARC before indexing it. This
preserves the original byte prefix without copying the old WARC into a partial.

The stable CDXJ contains the original CDX digest, status token, and URL key for
every response and revisit. Fetch can therefore reconstruct exact capture
inventory without rescanning WARCs. Incremental indexing removes prior lines for
changed filenames, adds their replacement lines, sorts the result, and validates
byte ranges against manifest sizes for shards that are not on disk. Archives
created without the required identity fields must be reset and regenerated.

## Local authority

With `authority = "local"`, the workspace is authoritative. Fetch appends
validated WARC members and atomically replaces CDXJ and manifest files. Navigator
may serve the same workspace directly. Normal publication preserves all previous
collections; `--reset-data` retains selected-collection reset behavior.

## Remote authority and bounded materialization

With `authority = "remote"`, the bucket prefix and its manifest are authoritative.
The workspace is a bounded working area. At startup Fetch reads the manifest.
Missing manifest means an empty archive. For each selected year it then:

1. Downloads the committed CDXJ if no local copy is present.
2. Queries IA and inventories the year.
3. Downloads that year's final WARC only when the year has new captures, keeping
   a local file that is already at least as large as the committed tail.
4. Runs the same local append and incremental-index code used by local authority.
5. Publishes changed/new artifacts and commits the manifest last.
6. Writes the run record, then deletes that collection's finalized local
   WARC/CDXJ files.

`captures/`, run diagnostics, and the small cached manifest remain local. A failed
upload keeps the workspace; the next run continues from those files.
Missing local artifacts are never interpreted as remote deletions. If the
workspace was lost after a partial same-key publication, reset and regenerate.

## Remote publication

After local WARC and CDXJ validation, one changed collection is published in this
order:

1. Upload the extended tail or newly rolled WARC. Unchanged WARC objects are
   skipped.
2. Replace the live CDXJ. A single S3 key is atomic, so readers see
   either all old bytes or all new bytes.
3. Replace `collections-manifest.json` last. This is the commit point.
4. Write the run record, then evict finalized local working files.

There is intentionally a short window after CDXJ replacement and before manifest
commit in which a fresh remote reader may reject the archive. Cached Navigator
instances continue with their last validated index; the next Fetch run republishes
if the workspace was retained. No versioned index keys or reader fallback protocol
are added.

## Destructive reset

Remote `--reset-data` is explicit authorization for whole-archive maintenance. It:

- rejects `--start` and `--end` overrides;
- prints a prominent deletion/playback-downtime warning;
- deletes only the descriptor's complete configured bucket prefix;
- clears only the exact workspace archive root; and
- rebuilds the descriptor's full configured range.

There is no interactive prompt. The old manifest is absent during the rebuild, so
remote playback is unavailable until publication completes. Other prefixes in the
same bucket are not touched.

## Module map

- `config.py`: descriptor path resolution and strict schema validation.
- `cli.py`: public argument contract and exit-code boundary.
- `fetch.py`: configured-history orchestration and yearly lifecycle.
- `console.py`: terminal progress, URL tables, colors, and Wayback links.
- `collection.py`: exact workspace paths, atomic files, and run records.
- `warc.py`: record construction, pre-append validation, append-only writing, and
  rollover.
- `index.py`: deterministic full or incremental collection CDXJ generation.
- `inventory.py`: CDXJ-driven identity inventory and within-collection revisits.
- `storage.py`: local manifests plus remote index/tail materialization,
  publication, and eviction.
- `cdx.py`, `resolution.py`, `workers.py`, `playback.py`: capture discovery and
  bounded playback acquisition.

## Verification

The Fetch test suite is run independently from Navigator:

```console
uv run pytest -q
```

Coverage includes descriptor conformance, the 250 MB default and configurable
rollover, deferred tail download (CDXJ for inventory, WARC only when a year has
new work), incremental indexing, publication order, successful eviction, failure
retention and retry, unchanged-artifact suppression, and reset prefix scope.
