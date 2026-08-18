# Archive Magic Fetch Architecture

## Purpose and boundary

Archive Magic Fetch turns Internet Archive capture history into portable WARC 1.1
collections and CDXJ indexes. It is a standalone producer. It does not call or
coordinate with Navigator; the two applications interact only through the archived
WARC/CDXJ layout in a flat directory.

One Fetch process on one machine owns an archive prefix. Concurrent writers,
secondary updater machines, and manual bucket mutation are unsupported.

## Public interface

```text
archive-magic-fetch ARCHIVE [--start DATE] [--end DATE] [--reset-data]
```

`ARCHIVE` is either a Fetch TOML file of any name or a directory containing
`fetch.toml`. There is no legacy URL-pattern, `--config`, or `--archives-root`
interface.

Normal CLI dates narrow a run without changing the configuration. Without overrides,
Fetch uses `[fetch].start` through `[fetch].end`; an omitted end is resolved to the
current UTC timestamp when the run starts. Fetch therefore checks the complete
configured history on daily or weekly runs. Identity-based deduplication makes
unchanged older collections no-ops, so normally only the current year is
republished.

## Configuration contract

The Fetch configuration is user-authored intent:

```toml
[archive]
id = "example.org"
url_pattern = "*.example.org"

[output]
type = "remote" # "local" or "remote"
data_directory = "data"
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
```

Rules:

- The format is unversioned but strict: unknown tables and keys are errors.
- Archive IDs must be route-safe and cannot be `.`, `..`, or `static`.
- Relative paths and `~` are resolved from the containing TOML file.
- `data_directory` is the exact managed artifact root. The archive ID is not appended.
  For remote output it remains the local working directory.
- Run records are written to `logs/` beside `data_directory`.
- Remote-only fields (`bucket`, `prefix`, `endpoint_url`, `region`) are required or
  optional as documented and are forbidden for local output. They are flattened
  into `[output]`, not nested.
- Bucket credentials come exclusively from Boto3's standard credential chain.
  No adjacent `.env` is loaded and no specific access-key variable is required.
- The compressed WARC rollover target defaults to 250,000,000 bytes and remains
  configurable per archive.
- `[fetch].retries` applies to both CDX and playback requests and defaults to four
  retries after the initial request. CDX retries are owned by Fetch: HTTP 429 and
  TCP connection refused pause for 60 seconds (or `Retry-After`) before the next
  attempt, matching playback backpressure. A CDX failure skips that year, continues
  with later years, and makes the process exit nonzero.

Fetch does not read Navigator configuration. Playback policy lives in `navigator.toml`.

## Data, logging, and publication layout

```text
<data_directory>/
  example.org-2004-001.warc.gz
  example.org-2004-002.warc.gz
  example.org-2004-index.cdxj

<data-directory-parent>/logs/
  <run-id>.json
  <run-id>.log
```

The data directory is a flat portable namespace. Strict artifact filenames retain
the logical yearly collection boundary without year folders. A collection exists
when `{archive_id}-{collection_id}-index.cdxj` is present. Prefix listings include
only those CDXJ and `.warc.gz` objects.

The JSON file under `logs/` combines the invocation's yearly run records; the text
file mirrors its console output. Both are local diagnostics rather than
bucket-authoritative content.

CDXJ files use same-directory temporary names followed by atomic local replacement.
WARC records are validated as independent gzip members before being appended to a
shard.

## Acquisition pipeline

For each year in the selected range, Fetch:

1. Ensures the year's CDXJ. Local output is a no-op; remote output
   downloads the committed CDXJ only if no local copy exists. Leftover local
   WARCs from an interrupted run are reindexed before inventory.
2. Queries Internet Archive CDX history through `WaybackClient.search()`, which
   owns parsing and resume-key pagination. Fetch owns CDX retries and treats
   connection refused like HTTP 429: pause 60s (or `Retry-After`) and retry the
   whole year query. Giving up on a year does not abort later years.
3. Parses and deduplicates captures by canonical capture identity.
4. Inventories existing captures from CDXJ identity metadata and skips those
   already represented.
5. If that year has captures not yet represented, remote output downloads
   only the CDXJ-referenced final WARC for the year (or keeps a longer local tail).
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
recovery tool when the data directory was lost or the prefix is confused.

Identical payloads reuse the oldest digest-matched full response, including
across years: a later capture with the same urlkey and digest becomes a
WARC revisit rather than another Internet Archive download. Empty payloads
still split on CDX status so a 301 and a 302 stay distinct. Years remain
publication partitions; the archive prefix is the portable unit. The
`payload-reuses` metric still counts only CDX-synthesized empty responses and
slash redirects. Local `--reset-data` of a subset of years can leave later
revisits referring to deleted records; remote `--reset-data` already wipes the
whole prefix.

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
byte ranges against listed WARC sizes for shards that are not on disk. Archives
created without the required identity fields must be reset and regenerated.

## Local output

With `output.type = "local"`, the data directory is authoritative. Fetch appends
validated WARC members and atomically replaces CDXJ files. Navigator may serve the
same data directly. Normal publication preserves all previous collections;
`--reset-data` retains selected-collection reset behavior.

## Remote output and bounded materialization

With `output.type = "remote"`, the bucket prefix is authoritative.
The data directory is a bounded working area. At startup Fetch lists the prefix.
For each selected year it then:

1. Downloads the committed CDXJ if no local copy is present.
2. Queries IA and inventories the year.
3. Downloads that year's CDXJ-referenced final WARC only when the year has new
   captures, keeping a local file that is already at least as large as the
   committed tail.
4. Runs the same local append and incremental-index code used by local output.
5. Publishes changed/new WARC objects and replaces the CDXJ last.
6. Deletes that collection's finalized local WARC/CDXJ files after commit.

Run records remain local. A failed upload keeps the data directory; the next run
continues from those files. Missing local artifacts are never interpreted as remote
deletions. If the data directory was lost after a partial same-key publication,
reset and regenerate.

## Remote publication

After local WARC and CDXJ validation, one changed collection is published in this
order:

1. Upload the extended tail or newly rolled WARC. Unchanged WARC objects are
   skipped via listed size and object metadata SHA-256.
2. Replace the live CDXJ. A single S3 key is atomic, so readers see
   either all old bytes or all new bytes. This CDXJ replacement is the collection
   commit point.
3. Write the year's run record, then evict finalized local working files.

An extended WARC is safe with an old index because all old byte ranges are
unchanged. Cached Navigator instances continue with their last validated index
until a changed index ETag is observed. No versioned index keys or reader fallback
protocol are added.

## Destructive reset

Remote `--reset-data` is explicit authorization for whole-archive maintenance. It:

- rejects `--start` and `--end` overrides;
- prints a prominent deletion/playback-downtime warning;
- deletes only the configuration's complete configured bucket prefix;
- clears only the exact managed data directory; and
- rebuilds the configuration's full configured range.

There is no interactive prompt. Remote playback is unavailable until publication
completes. Other prefixes in the same bucket are not touched.

## Module map

- `config.py`: Fetch-local TOML loading, path resolution, and safety checks.
- `cli.py`: public argument contract and exit-code boundary.
- `fetch.py`: configured-history orchestration and yearly lifecycle.
- `console.py`: terminal progress, URL tables, colors, and Wayback links.
- `collection.py`: flat managed-data paths and atomic artifact files.
- `warc.py`: record construction, pre-append validation, append-only writing, and
  rollover.
- `index.py`: deterministic full or incremental collection CDXJ generation.
- `inventory.py`: CDXJ-driven identity inventory and identical-payload revisits.
- `storage.py`: remote prefix inventory plus index/tail materialization,
  publication, and eviction.
- `cdx.py`, `resolution.py`, `workers.py`, `playback.py`: capture discovery and
  bounded playback acquisition.

## Verification

The Fetch test suite is run independently from Navigator:

```console
uv run pytest -q
```

Coverage includes configuration conformance, the 250 MB default and configurable
rollover, deferred tail download (CDXJ for inventory, WARC only when a year has
new work), incremental indexing, publication order, successful eviction, failure
retention and retry, unchanged-artifact suppression, and reset prefix scope.
