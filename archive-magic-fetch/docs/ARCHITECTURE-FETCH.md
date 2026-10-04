# Archive Magic Fetch Architecture

## Purpose and interface

Fetch turns Wayback or Common Crawl capture history into annual WARC 1.1 collections
and CDXJ indexes. Navigator reads that flat file format independently.

```text
archive-magic-fetch ARCHIVE [--config PATH] [--start DATE] [--end DATE] [--reset-data]
  [--workers N] [--starts-per-second N] [--retries N] [--trace-requests]
archive-magic-fetch ARCHIVE --sync-only
```

`ARCHIVE` is a TOML path or a directory containing `fetch.toml`. Playback
workers, start rate, and retries are host policy, configured independently for each
source. They come from `~/.config/archive-magic-fetch/fetch-config.toml`
(or `$XDG_CONFIG_HOME/archive-magic-fetch/fetch-config.toml`). If missing, the file
and its parent directories are created with these defaults:

```toml
[wayback]
workers = 4
starts_per_second = 8
retries = 4

[common-crawl]
workers = 4
starts_per_second = 8
retries = 4
```

`--config PATH` takes precedence over `ARCHIVE_MAGIC_FETCH_CONFIG`, which takes
precedence over the default path. A missing file is created at the selected path;
existing files are left unchanged. Both source sections require all three settings,
with no shared defaults or inheritance between sections. The archive's
`[archive].source` selects its policy, and CLI flags override that policy for one
run. These fields do not belong in per-archive `fetch.toml`.

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

This writes `logs/<run>.requests.csv` beside the normal run log, with a header and
one row per request, flushed when its response headers or transport error arrive.
`start_utc` is the actual UTC start time rounded to milliseconds; `duration_ms` is
an integer number of milliseconds measured with the monotonic clock. Duration
covers headers only; streamed payload reads happen afterward. `capture_time` is
the historical capture date and `digest` is its last six characters, matching the
progress log. The SURT URL key is omitted.

Request ID, timing, HTTP status, capture date/digest, attempt number, request number
within the attempt, and rolling 1s/60s counts are on the left. Counts are sampled
at request start. Method and process ID follow, then variable-width fields:
thread, Retry-After, error type, request URL, and redirect Location. CSV quoting
preserves commas, quotes, and newlines within fields. Rows are appended in
completion order; sort by request ID to recover start order with multiple workers.
On orderly shutdown, unfinished requests get one row marked `Interrupted` with
blank duration and status. Totals and peak rates remain in the normal run log,
without adding non-request rows to the CSV.

Repeated URLs with increasing `attempt` values are capture retries. A
`request_in_attempt` greater than 1 exposes redirect requests. Capture
timestamps in ordinary progress output are historical capture dates, so they
cannot establish the real request rate. Trace files contain full requested URLs.

To count sends in each UTC calendar second and minute (rather than rolling windows):

```sh
python - /path/to/logs/RUN.requests.csv <<'PY'
import collections, csv, sys
buckets = {"second": collections.Counter(), "minute": collections.Counter()}
with open(sys.argv[1], newline="") as stream:
    for row in csv.DictReader(stream):
        buckets["second"][row["start_utc"][:19]] += 1
        buckets["minute"][row["start_utc"][:16]] += 1
for unit, counts in buckets.items():
    for timestamp, count in sorted(counts.items()):
        print(unit, timestamp + "Z", count)
PY
```

### Archive configuration

The `--sync-only` form requires remote output and does not query either source. It cannot
be combined with dates or `--reset-data`.

The existing TOML fields stay in place:

```toml
[archive]
id = "example.org"
url_pattern = "*.example.org"
source = "wayback" # default; or "common-crawl"

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

For Wayback, complete historical years are cached as ordinary JSON arrays in
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

After acquisition, playback captures are filtered by the requested dates,
deduplicated by identity, and sorted. Each CDX URL group is assigned to one worker,
which walks its captures
chronologically. There is no pre-pass selecting unique payloads or choosing
which groups need downloads. The worker owns a map of successful digests to
stored response references, seeded from earlier successful responses for that
URL in the same annual collection. It checks that map at each capture:

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
across different URL groups or years. Each year stores its own full response for
each successfully acquired URL/digest, even when another year has identical bytes.
Workers do not mutate each other's maps.

Exact playback does not follow Wayback's nearby-capture substitutions, synthesize
slash redirects, or manufacture empty responses from CDX. Previously failed
captures remain failures even if a later capture with the same digest succeeds.
Revisits refer only to earlier successful responses in the same year. A staged
write failure aborts the year; only successfully promoted data seeds subsequent
updates to that year. Resetting one year cannot invalidate another year's revisits.

Resolution and writing share an explicit annual worker batch. On failure or
Ctrl-C, it cancels queued groups, stops active groups before new captures,
attempts, or transport sends, and wakes retry and pacing waits. In-flight HTTP
operations finish under their existing timeouts. The batch drains before staging
is discarded or the next year begins. Cancellation bypasses source failure
classification; real backpressure already observed remains in force. Persistent
worker clients remain open across successful and failed batches until run cleanup.

For each year, Fetch creates a same-filesystem stage. Unchanged WARC shards
are hard-linked into it; the final shard is copied only if new captures need to
append to it, and the CDXJ is copied. The
existing serialized WARC writer appends validated gzip members to that stage,
and the CDXJ indexer validates the resulting byte ranges. On failure or Ctrl-C,
Fetch discards unfinished staged work, leaving finalized local files unchanged.

After validation, Fetch writes a small ready record in the stage, promotes
changed WARCs, then promotes the CDXJ. A later fetch or manual sync completes
a promotion interrupted between these steps before touching the bucket.
Unchanged years do not replace their canonical files. Indexing owns the decision
to reuse an existing CDXJ, update changed shards, or rebuild a missing index.
An explicit full rebuild remains available to archive-wide reconciliation.

The default compressed WARC target is 250,000,000 bytes. Normal updates only
extend a year's final shard or create a new shard; the previous byte prefix and
CDXJ offsets remain valid. Older yearly shards are not rewritten.

## Common Crawl acquisition

Set `[archive].source = "common-crawl"` to select CC; omitted source remains
`"wayback"`. Unknown values are configuration errors. There is one source per
archive configuration and no automatic fallback or simultaneous source merging.
Changing source or query does not remove existing output: use a separate archive
directory for an isolated collection. Sync-only constructs no source client.
Wayback's `cdx_page_limit` and `cdx_window_days` do not tune CC discovery.

### Complete annual discovery and cache freshness

Fetch reads a fresh [catalog](https://index.commoncrawl.org/collinfo.json) once
per run and chooses collections by their actual UTC coverage bounds, not by the
year in the collection ID. It queries every page of every overlapping collection
with `showNumPages` and consistent `pageSize=5` (compressed index blocks, not
rows). Status and MIME filters and digest collapse are not applied. Query syntax
supports the same exact, domain-wildcard, and prefix-wildcard forms as Wayback.
When shared normalization leaves the match type unspecified, the request omits
`matchType`, preserving CDX inference for patterns such as `https://example.org/news*`.
Its cache key reflects those actual query parameters, so earlier forced-exact
entries are not reused for corrected queries and can remain on disk.
Only matching captures available in the chosen source can be acquired; this does
not guarantee complete historical coverage of a website.

Every query and cache entry covers January 1 00:00:00 through December 31
23:59:59 UTC of the requested year. Configured and CLI dates filter the complete
listing afterward. A June-only run therefore cannot populate a June-only annual
cache. Collections are ordered by coverage start and ID, pages numerically, and
records in response order; shared deduplication retains the first complete
reference for each identity.

CC caches are versioned JSON envelopes at
`index/common-crawl/<query-hash>/<crawl-id>/<year>.json`, outside the resettable
data directory. They contain normalized query, full-year bounds, collection
metadata, and capture references including byte-range locators. Only complete
per-crawl queries are published atomically. A failed page or crawl prevents an
annual listing from reaching WARC work; already completed crawl caches survive.
Valid empty results are cached. Page counts precede date filtering, so a numbered
page's recognized no-captures 404 is also a valid empty result; other HTTP or
parsing errors still fail discovery. Corrupt entries fail explicitly, without silently
refetching or replacing them. Wayback cache paths and arrays are unchanged.

Completed entries are reused even for the current year. A new run's catalog adds
newly published collections and invalidates entries whose collection metadata
changed. This is metadata-only freshness: updates within an existing collection
with unchanged metadata are not detected. Remove its cache entry to force a
refresh, or remove the CC query directory to refresh that query. Changed queries
use separate hashes. Resetting data does not clear discovery caches.

Index requests are sequential, paced at one start per second, separate from
acquisition metrics. Connection failures, timeouts, 429, and 5xx responses have
five total attempts, with 60-second exponential delays capped at 600 seconds,
honoring longer `Retry-After`. Connection/read timeouts are 10/120 seconds. A
failed catalog is never interpreted as empty coverage.

### Ranges, validation, and supported records

Worker clients use HTTPS ranges from `data.commoncrawl.org` without AWS
credentials. Each request selects `offset` through `offset + length - 1` and
requires HTTP 206, matching range metadata, and the exact compressed byte count.
Reads are bounded to the advertised length plus one byte; ignored ranges,
redirects, and unexpected outer content encoding are rejected before consuming
an unbounded body. Responses close on success, failure, or interruption.

Validation first uses independent gzip decompression with completed member,
CRC, and trailer-size checks. Missing trailers, extra members, and trailing
garbage fail. Then warcio parses one WARC record, with explicit block-length and
framing checks and independent verification of every supplied block and payload
digest. Both WARC and HTTP header boundaries are measured from raw bytes, not
warcio's decoded-character counts, so UTF-8 headers retain correct byte extents. Parser EOF or an aggregate digest flag alone does not establish validity.
Target URL, timestamp, and available original HTTP status must match discovery.
The HTTP 206 of the range transport is not the original response status.

CC already removes HTTP content and transfer encoding. Read `raw_stream`
without interpreting stale encoding headers, as documented in its
[format notes](https://github.com/commoncrawl/arc2warc-conversion/blob/main/README.md#required-rewriting-of-http-headers).
The shared header helper accepts ordered pairs; CC retains repeated headers such
as `Set-Cookie`, and Wayback supplies its mapping's `.items()`. Obsolete
representation headers are removed and output length/digest reflect stored bytes.
An index-only digest mismatch can retain a valid exact response, but cannot seed
reuse. Missing digests cannot seed reuse either. A failed supplied WARC digest
is an acquisition failure, not an accepted index mismatch.

Support is limited to complete WARC 1.0/1.1 response records (CC's WARC era,
starting in 2013). Legacy ARC, segmented or non-response records, declared
truncation, and unresolved source revisits are explicit failures. Shared
same-year reuse can still satisfy a capture without acquisition. External revisit
chains are never copied into output or fetched to reconstruct payloads.

Strict rejection without historical repair excludes some otherwise recoverable
captures. For example, `CC-MAIN-2018-34` has an
[extra CRLF defect](https://commoncrawl.org/errata/extra-line-in-response-records-between-headers-and-payload)
between HTTP headers and payload. Records failing length or digest verification
remain failures; Fetch does not strip bytes to repair them or reject the entire
collection by its ID. Old crawls may also omit truncation metadata, so structural
and digest validation cannot establish that every original website response was
fully crawled. Wayback-specific stub detection and newline tolerance do not apply.

Retrieval and decoding are one shared acquisition attempt. Connection/read
timeouts are 10/60 seconds. Host worker/rate/retry policy applies; transport
retries are disabled. Corrupt records, incomplete transfers, timeouts, connection
failures, 429, and 5xx may retry within that budget. Invalid locators, identity
mismatches, unsupported types, declared source truncation, and permanent HTTP
failures do not. Per-capture delays start at five seconds and double to 60 seconds,
honoring longer `Retry-After`. HTTP 429/503 and connection refusal pause the pool
for at least 60 seconds or a longer requested delay. Cancellation-aware waits,
worker draining, and late backpressure preservation remain shared behavior.

### Optional local smoke procedure

Automated tests use generated source records and fake HTTP responses. For an
explicitly opted-in live check, create a fresh directory with this `fetch.toml`:

```toml
[archive]
id = "cc-smoke"
source = "common-crawl"
url_pattern = "https://commoncrawl.org/"

[output]
type = "local"
data_directory = "data"

[fetch]
start = "2024-06-01"
end = "2024-06-30"
```

Run `uv run archive-magic-fetch /path/to/cc-smoke --workers 1 --starts-per-second 1
--retries 1 --trace-requests` on one shell line. Discovery still covers the full
calendar year for this exact URL. Inspect the run log, request trace, WARC/CDXJ,
and any explicit unresolved records. Repeat to check resume. This example can
legitimately select no captures; choose another exact URL or short period if
needed. It writes only local output and never publishes to a bucket.

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
without contacting either source.

## Reset and migration

Local `--reset-data` rebuilds selected years through staging. Remote
`--reset-data` remains a destructive full-range operation: it rejects date
overrides, warns of playback downtime, deletes only managed files for this archive
under remote `data/`, preserving metadata, assets and unrelated objects,
clears the local data directory, and rebuilds and publishes years in order.
Both reset modes preserve discovery caches and reuse them to rebuild WARC
contents. A successful empty selection can clear a local year through reset
staging. WARC failures also leave completed CDX caches intact.

Before switching an existing bucket-authoritative archive, finish pending
publication with the old Fetch version. Restore its WARC/CDXJ objects into a
fresh local `data_directory` once using rclone, and compare the local and
remote file sets. Fetch performs no bucket download during normal operation.

For existing flat bucket layouts, follow the [migration guide](../../docs/BUCKET-CATALOG-MIGRATION.md).
Fetch does not migrate or delete old root objects automatically.

## Code organization

Fetch is organized by pipeline stage. Workflow modules expose one primary
operation near the top, followed by private helpers. Shared records, cohesive
stateful classes, and reusable primitives may expose the related operations they
need. Internal Python import paths are not a compatibility interface; the CLI,
configuration, caches, and published archive format are unchanged.

Action-oriented Python modules use lowercase `verb_noun.py` names, usually
matching their primary operation: `build_collection_index.py`,
`resolve_captures.py`, and `write_captures.py`. Use a specific action and object
instead of a gerund or a generic name such as `stage.py`. Tests follow the same
names with a `test_` prefix. Modules that define shared concepts may retain clear
noun names (`models.py`, `contracts.py`, `layout.py`, `identity.py`, `dates.py`,
and `format.py`); conventional `cli.py`, `__init__.py`, and `conftest.py` names
remain appropriate. Package directories group responsibilities.

```text
archive_magic_fetch/
  cli.py                            parse arguments and dispatch
  run_application.py                configure fetch or sync-only
  models.py                         capture, outcome, artifact, and metrics records
  contracts.py                      source callbacks and failure advice
  config/
    load_archive_config.py          load and validate archive configuration
    load_playback_policy.py         load host acquisition policy
    build_settings.py               combine configuration and overrides
    read_toml_section.py            read and validate a TOML section
    models.py                       configuration records and defaults
  adapters/
    build_wayback_source.py         bind the Wayback implementation
    build_common_crawl_source.py    bind CC discovery and worker clients
    query_common_crawl_index.py     run-scoped catalog and index request policy
    interpret_common_crawl_failures.py  CC acquisition failure advice
    create_wayback_client.py        construct clients and repair transport
    interpret_wayback_failures.py   interpret failures and replay-specific rules
  pipeline/
    run_fetch.py                    coordinate annual work
    discovery/
      discover_captures.py          select an ordered unique set
      load_or_fetch_year_cdx.py     acquire complete Wayback listings with caching
      load_or_fetch_common_crawl_year.py  acquire complete per-crawl annual queries
    retrieval/
      fetch_capture.py              execute and retry an acquisition
      retrieve_memento.py           request exact replay
      retrieve_warc_range.py        request and bound one CC compressed byte range
    decoding/
      decode_memento.py             validate and normalize the Wayback response
      decode_warc_capture.py        strictly validate and normalize a CC record
    resolve_captures.py             reuse stored payloads or acquire captures
    write_captures.py               serialize and append WARCs
    build_collection_index.py      construct local CDXJ
    reconcile_missing_indexes.py   repair indexes through the same indexer
    stage_year.py                   prepare, commit, abort, and recover a year
    publication/
      sync_archive.py               publish committed artifacts
      purge_remote.py               explicitly reset managed data
      run_rclone.py                 configure and invoke rclone
  archive/
    inventory_collection.py        read stored captures and reusable responses
    validate_local_archive.py      validate finalized artifacts for publication
    layout.py                       archive paths and artifact inventories
    identity.py                     capture identity and digest primitives
    normalize_cdx_search.py          shared domain/prefix/exact query syntax
    dates.py                        date bounds and annual partitions
    format.py                       archive fields and CDXJ primitives
  runtime/
    manage_capture_workers.py      schedule bounded work with persistent clients
    track_http_requests.py         instrument HTTP sends and collect statistics
    pace_requests.py               coordinate request starts and cooldowns
    calculate_retry_delay.py       interpret retry timing and wrapped errors
    manage_archive_files.py        lock archives and publish files atomically
    report_progress.py             render progress and mirror run logs
    write_run_record.py            initialize and write structured run records
```

Application setup selects the adapter. The runner and shared stages depend on
neutral contracts; they do not construct Wayback clients or inspect Wayback
exceptions. Archive and runtime support do not import pipeline stages. Discovery
reads the source index; output indexing constructs our archive's CDXJ. They remain
separate despite both using CDX terminology.

```text
Discover -> Resolve -> Write -> Index -> Commit -> Publish
                |
                +-- when needed: Retrieve -> Decode
```

These are responsibility boundaries, not whole-run buffering barriers. Resolution
walks each URL group chronologically. Bounded workers acquire missing captures,
and one writer consumes the ordered outcomes. A successful response can enable
later revisits within the same year. Local commit and remote
publication retain separate failure and recovery boundaries. Sync-only uses the
same transaction recovery and publication code as fetch.

### Source contract

`SourceAdapter` is a typed bundle of callables, assembled with composition:

- `discover(request)` returns a complete `CaptureListing` and query metadata.
- `open_client(stats)` is a context manager creating one persistent worker client;
  the source explicitly installs transport instrumentation before yielding it.
- `fetch(client, capture)` performs one retrieval-and-decoding attempt and returns
  a normalized `CaptureResult`. It receives the complete `CaptureRef`, not just
  its identity.
- `preflight(capture)` may return a failure without starting an attempt.
- `failure_advice(error, attempt)` supplies a neutral category, retry decision,
  delay, coordinated cooldown, and optional failure-group limit.
- `capture_link(capture)` receives the complete `CaptureRef` and supplies the source URL used in terminal links. Capture outcomes carry that reference through reporting.

The retrieval stage owns attempts and counters. The source owns interpretation:
Wayback stub digests, exact-capture rules, newline digest tolerance, and exception
classification stay in its implementation. Retry advice preserves the distinction
between pool cooldowns and per-capture timeouts. CDX window retries and splitting
remain within Wayback discovery, independently of capture retry settings.

The Wayback decoder closes its Memento even when reading fails or is interrupted.
False-gzip repair remains at the session boundary, before the upstream client can
consume a misleadingly encoded response; a repair failure or interruption closes
the response there before propagating the error. Worker clients close after
outstanding work finishes, and request traces close even if client cleanup fails.

Capture identity, Wayback cache serialization, and the public archive format are
unchanged. `CaptureRef` adds an optional typed Common Crawl locator (crawl ID,
filename, offset, length); that locator is retained in CC discovery caches but is
not part of capture identity. Failure display labels are transient; existing
run-record fields and trace column names remain unchanged. CC query metadata in
run records identifies the source and selected collections. `WARC-Source-URI`
records the source WARC URL, while terminal capture links open its collection's
index query rather than offering a fictitious replay URL.

Archives previously written with cross-year revisits must be discarded and
rebuilt before using annual independence. No conversion is provided. Historical
discovery caches can be retained for the rebuild.

### Verification

Tests are grouped under `adapters/`, `archive/`, `config/`, `pipeline/`, `runtime/`,
and `integration/`. A fake source exercises the complete shared pipeline, including
resume and revisits, without using Wayback acquisition. Dedicated tests cover
response/client cleanup and retries spanning response decoding. Existing fixtures
continue to cover cache boundaries, transport pacing, transaction recovery, and
ordered rclone publication. Run Fetch and Navigator suites separately:

```console
.venv/bin/python -m pytest archive-magic-fetch/tests -q
.venv/bin/python -m pytest archive-magic-navigator/tests -q
```
