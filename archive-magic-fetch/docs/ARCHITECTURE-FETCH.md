# Archive Magic Fetch Architecture

## Purpose and interface

Fetch turns Internet Archive capture history into annual WARC 1.1 collections
and CDXJ indexes. Navigator reads that flat file format independently.

```text
archive-magic-fetch ARCHIVE [--config PATH] [--start DATE] [--end DATE] [--reset-data]
  [--workers N] [--starts-per-second N] [--retries N]
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

Fetch visits years in order. It queries the whole-year CDX through the existing
`wayback` client. Playback `--retries` does not apply to CDX. HTTP 504, read
timeouts, 429, and connection refused retry the same window ten times with
exponential pauses from 60s capped at 10 minutes (about an hour). If that
year still has no listing, Fetch records a hole, skips downloads for that
year, and continues with the next year. Date splitting is reserved for a
300-second wall-clock budget, which means the window itself is too expensive:
first `cdx_window_days` slices (default 28), then 7-day slices. A 7-day
window that still fails is a hole. A hole is recorded in
`logs/cdx/{year}.json` with every completed sibling window. The next run
queries only the holes. Fetch skips memento work and publication until the
year listing is complete, then deletes the checkpoint. A complete CDX listing
is deduplicated by capture identity. The existing URL-owned playback workers,
chronological ordering within each URL, retry policy, and cross-year digest
representatives remain in use. Individual unresolved mementos retain the
existing skip-and-record policy.

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
- `cdx.py`: CDX search, failure classification, window splits, and hole checkpoints.
- `resolution.py`, `workers.py`, and `playback.py`: Wayback playback acquisition.
