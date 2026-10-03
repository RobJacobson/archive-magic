# Archive Magic Fetch Architecture

## Purpose and interface

Fetch turns Internet Archive capture history into annual WARC 1.1 collections
and CDXJ indexes. Navigator reads that flat file format independently.

```text
archive-magic-fetch ARCHIVE [--config PATH] [--start DATE] [--end DATE] [--reset-data]
  [--workers N] [--starts-per-second N] [--retries N] [--trace-requests]
archive-magic-fetch ARCHIVE --sync-only
```

`ARCHIVE` is a TOML path or a directory containing `fetch.toml`. Playback
workers, start rate, and retries are host policy. They come from
`~/.config/archive-magic-fetch/fetch-config.toml` when that file exists:

```toml
[playback]
workers = 4
starts_per_second = 8
retries = 4
```

`--config PATH` or `ARCHIVE_MAGIC_FETCH_CONFIG` selects another file. CLI flags
override the file for one run. A missing file keeps the code defaults: 4
workers, 16 starts/second, and 4 retries. These fields do not belong in
per-archive `fetch.toml`.

### Playback request diagnostics

`starts_per_second` controls HTTP transport sends shared across a run's workers.
At 2, sends are spaced at least 0.5 seconds apart. The gate sits immediately before
the Requests HTTP adapter sends, so redirects and capture retries each consume
a slot. Wayback and urllib3 automatic
retries are disabled. `retries = 4` allows up to five capture attempts; one attempt
can issue several HTTP requests. Existing `playback_attempts` metrics still count
capture attempts. Duplicate capture identities and reusable revisits require no
additional HTTP requests. The limit is per process, and CDX requests use their
own pacing and are excluded from these playback counters.

The startup line prints effective worker/rate/retry settings. Every run prints a
`playback HTTP` summary with total sends, peak counts in rolling 1s and 60s windows,
and the number of 429 responses. Each 429 prints its request ID and recent counts
before the existing cooldown message. Windows are `(now - window, now]`; failed
connection attempts count as sends. These counters measure attempted HTTP sends,
not packets or successful payload downloads.

For a detailed trace:

```console
uv run archive-magic-fetch /path/to/archive --trace-requests
```

This writes a flushed `logs/<run>.requests.jsonl` beside the normal run log. Every
`request_start` has a real `time_utc`, monotonic timestamp, process/thread ID,
request ID, method/URL, requested capture identity, attempt number, request number
within that attempt, and recent/peak counts. A matching `request_end` has status,
Retry-After, Location, or transport exception type. Response elapsed time covers
headers only; streamed payload reads happen afterward. Interrupted sends may have
only a start event. A final `summary` is written when workers shut down.

Repeated URLs with increasing `attempt` values are capture retries. A
`request_in_attempt` greater than 1 exposes redirect requests. Capture
timestamps in ordinary progress output are historical capture dates, so they
cannot establish the real request rate. Trace files contain full requested URLs.

To count sends in each UTC calendar second and minute (rather than rolling windows):

```sh
python - /path/to/logs/RUN.requests.jsonl <<'PY'
import collections, json, sys
buckets = {"second": collections.Counter(), "minute": collections.Counter()}
with open(sys.argv[1]) as stream:
    for line in stream:
        event = json.loads(line)
        if event["event"] == "request_start":
            buckets["second"][event["time_utc"][:19]] += 1
            buckets["minute"][event["time_utc"][:16]] += 1
for unit, counts in buckets.items():
    for timestamp, count in sorted(counts.items()):
        print(unit, timestamp + "Z", count)
PY
```

### Archive configuration

The `--sync-only` form requires remote output and does not query Wayback. It cannot
be combined with dates or `--reset-data`.

The existing TOML fields stay in place:

```toml
[archive]
id = "example.org"
url_pattern = "*.example.org"

[output]
type = "remote" # or "local"
data_directory = "data"
bucket = "archive-magic"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"

[fetch]
start = "1995-01-01"
# end omitted means now
# warc_target_bytes = 250000000
# cdx_page_limit = 5000
# cdx_window_days = 28
```

Remote output now means **local-authoritative with a bucket mirror**. The
`data_directory` contains every finalized WARC and CDXJ. Fetch never downloads
these artifacts from the bucket and never evicts them. The bucket, prefix,
endpoint, and region configure a temporary rclone S3 backend; rclone obtains
credentials from the AWS environment or profile. Missing credentials fail
immediately instead of probing EC2 instance metadata. Rclone must be installed
on the Fetch host.

Run records and the process lock live in `logs/` beside `data_directory`.
The hidden `data/.staging/` directory holds one year's unfinished work and is
excluded from publication. The public archive format remains a flat directory:

```text
data/
  example.org-2004-001.warc.gz
  example.org-2004-002.warc.gz
  example.org-2004-index.cdxj
```

## Annual acquisition

Fetch visits selected years in order, taking the current UTC year once at run
start and skipping future years. CDX queries always cover January 1 00:00:00
through December 31 23:59:59. Exact configured/CLI dates filter playback captures
after acquisition; they do not narrow the CDX query.

Complete historical years are cached as ordinary JSON arrays in
`index/YYYY.cdx.json` beside `fetch.toml`. An existing valid cache avoids all CDX
requests for that year. Each record contains `urlkey`, `original_url`, `timestamp`,
`status_token`, `payload_digest`, and `mime`; `[]` represents a successfully
queried empty year. Fetch saves a cache only after every page and fallback slice
succeeds, writing a temporary sibling and atomically renaming it before WARC work.
Temporary files are never cache hits. Invalid JSON, fields, timestamps, or records
from the wrong year fail that year without replacing or re-fetching the cache.
The current UTC year is always queried afresh and never reads or writes a
persistent cache, even when playback dates select only completed months.

The pinned `wayback` client still handles pagination and shared CDX pacing.
Playback `--retries` does not apply to CDX. HTTP 504, read timeouts, 429, and
connection refused retry the same window up to ten attempts with exponential
pauses from 60s capped at 10 minutes, honoring a longer `Retry-After`. Other
transient failures retain their three-attempt budget. A 300-second wall-clock
budget triggers smaller windows: first `cdx_window_days` (default 28), then
7-day windows when smaller. Fallback slices run sequentially in memory. A
terminal failure stops that year immediately, discards its results, skips WARC
processing, and continues to later years with a nonzero final exit status.
Failed acquisitions retain no progress between runs.

There is no coverage manifest, refresh mechanism, or persisted partial checkpoint.
Legacy files in `logs/cdx/` are ignored and left untouched. The normalized URL
query must remain fixed for a cache's lifetime; explicitly clear the cache before
changing it. Cached historical years are retained indefinitely, accepting that
captures made visible later, including around the UTC year boundary, are missed.
A completed acquisition is not a guarantee of permanent upstream completeness.

The cache stays beside `fetch.toml` even with a custom data directory. Programmatic
settings may provide `index_directory`; its default is `data_directory.parent /
"index"`. The resolved cache directory must be outside the data directory, so
resetting data cannot delete it. The existing archive lock enforces one process
per archive layout; no separate cache lock is added. Concurrent processes sharing
a cache through different archive layouts are unsupported. Only WARC/CDXJ data
is published to the bucket.

After acquisition, requested playback captures are sorted and deduplicated by
identity. Each CDX URL group is assigned to one worker, which walks its captures
chronologically. There is no pre-pass selecting unique payloads or choosing
which groups need downloads. The worker owns a map of successful digests to
stored response references, seeded from earlier successful responses for that
URL in the archive. It checks that map at each capture:

1. An already stored capture needs no work.
2. A digest already obtained at or before this timestamp produces a lightweight
   revisit, preserving the capture date without another HTTP request.
3. Otherwise, attempt exact playback with the configured retry policy. A
   successful digest-matched response enters the map. A failure is recorded and
   the next capture is attempted, even if it advertises the same digest.

Digest mismatches may still be retained for their own exact capture, but never
mark the expected digest as obtained. Missing digests cannot deduplicate. The
existing status distinction for empty payloads keeps empty 301 and 302 responses
separate. Digest reuse is scoped to a CDX URL key, never shared solely by hash
across different URL groups. Workers do not mutate each other's maps.

Exact playback does not follow Wayback's nearby-capture substitutions, synthesize
slash redirects, or manufacture empty responses from CDX. Previously failed
captures remain failures even if a later capture with the same digest succeeds.
Revisits refer only to earlier successful responses. A staged write failure
aborts the year; only successfully promoted data seeds later years and runs.

For each year, Fetch creates a same-filesystem stage. Unchanged WARC shards
are hard-linked into it; the final shard is copied only if new captures need to
append to it, and the CDXJ is copied. The
existing serialized WARC writer appends validated gzip members to that stage,
and the CDXJ indexer validates the resulting byte ranges. On failure or Ctrl-C,
Fetch discards unfinished staged work, leaving finalized local files unchanged.

After validation, Fetch writes a small ready record in the stage, promotes
changed WARCs, then promotes the CDXJ. A later fetch or manual sync completes
a promotion interrupted between these steps before touching the bucket.
Unchanged years do not replace their canonical files. Successful years update
the cross-year representative map only after promotion.

The default compressed WARC target is 250,000,000 bytes. Normal updates only
extend a year's final shard or create a new shard; the previous byte prefix and
CDXJ offsets remain valid. Older yearly shards are not rewritten.

## Publication

Fetch waits for the completed year's rclone reconciliation before starting the
next year. Automatic reconciliation is limited to that year's managed filenames;
`--sync-only` reconciles every annual WARC/CDXJ in the local archive. Both use
a preflight listing of the archive root to reject legacy flat WARC/CDXJ objects,
then the same three ordered passes targeting `<bucket>/<prefix>/data/`:

1. Copy WARCs to the bucket without deleting remote files.
2. Sync CDXJ files, which makes the newly uploaded records visible.
3. Sync WARCs, deleting obsolete remote WARC files only after index publication.

Only files at the root of `data/` for the configured archive are eligible.
The configured prefix identifies the archive root; `data/` is appended automatically.
Metadata and images are manually published siblings and are never managed by sync. Logs, staging,
and unrelated bucket keys are excluded. A missing or empty local archive
causes sync to fail rather than delete the bucket. Sync validates local CDXJ
ranges against local WARC sizes before publication. A fetch and a manual sync
cannot run concurrently on the same archive.

An acquisition failure skips the affected year and allows later years to run.
An rclone failure stops the run immediately. Local completed files stay
available; `archive-magic-fetch ARCHIVE --sync-only` retries publication
without contacting Wayback.

## Reset and migration

Local `--reset-data` rebuilds selected years through staging. Remote
`--reset-data` remains a destructive full-range operation: it rejects date
overrides, warns of playback downtime, deletes only managed files for this archive
under remote `data/`, preserving metadata, assets and unrelated objects,
clears the local data directory, and rebuilds and publishes years in order.
Both reset modes preserve historical CDX caches and reuse them to rebuild WARC
contents. A successful empty selection can clear a local year through reset
staging. WARC failures also leave completed CDX caches intact.

Before switching an existing bucket-authoritative archive, finish pending
publication with the old Fetch version. Restore its WARC/CDXJ objects into a
fresh local `data_directory` once using rclone, and compare the local and
remote file sets. Fetch performs no bucket download during normal operation.

For existing flat bucket layouts, follow the [migration guide](../../docs/BUCKET-CATALOG-MIGRATION.md).
Fetch does not migrate or delete old root objects automatically.

## Modules

- `fetch.py`: annual CDX, playback, deduplication, and promotion orchestration.
- `staging.py`: annual copy-on-write staging and interrupted-commit recovery.
- `storage.py`: archive lock, local preflight, and ordered rclone commands.
- `warc.py` and `index.py`: portable WARC and CDXJ construction.
- `cdx.py`: CDX search, failure classification, window splits, and complete historical-year caches.
- `resolution.py`, `workers.py`, and `playback.py`: Wayback playback acquisition.
