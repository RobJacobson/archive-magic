# Archive Magic Fetch Architecture

## Purpose and interface

Fetch turns Wayback or Common Crawl capture history into annual WARC 1.1 collections
and CDXJ indexes. Navigator reads that flat file format independently.

```text
archive-magic-fetch ARCHIVE [--config PATH] [--start DATE] [--end DATE] [--reset-data]
  [--workers N] [--starts-per-second N] [--retries N] [--trace-requests]
archive-magic-fetch ARCHIVE --sync-only
```

`ARCHIVE` is a bare collection name under `~/archive-magic/collections/`, an
explicit directory (such as `./example.org`), or a TOML path. Bare names always
select the user workspace, even if a same-named directory exists in the current
working directory. Playback
workers, start rate, and retries are host policy, configured independently for each
source. They come from `~/archive-magic/fetch-config.toml`
(independent of `$XDG_CONFIG_HOME`). If missing, the file
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
run. These fields do not belong in per-archive `collection.toml`.

### Playback request diagnostics

`starts_per_second` controls playback and Wayback CDX HTTP transport sends shared
across a run's workers.
At 2, sends are spaced at least 0.5 seconds apart. The gate sits immediately before
the Requests HTTP adapter sends, so redirects and capture retries each consume
a slot. Wayback and urllib3 automatic
retries are disabled. `retries = 4` allows up to five capture attempts; one attempt
can issue several HTTP requests. Existing `playback_attempts` metrics still count
capture attempts. Duplicate capture identities and reusable revisits require no
additional HTTP requests. The limit is per process. Wayback CDX searches, pages,
retries, and redirects use the same gate and counters as playback. CDX keeps its
2.5-second endpoint minimum; both deadlines are reserved atomically so a prior
cooldown cannot create a burst. This also spaces the CDX-to-playback transition.
Common Crawl discovery retains its separate pacing and counters.

The startup line prints effective worker/rate/retry settings. Every run prints a
`HTTP` summary with total sends, peak counts in rolling 1s and 60s windows,
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
progress log. The SURT URL key is omitted. `phase` is `cdx` or `playback`. CDX rows
leave capture fields empty; `attempt` identifies the query retry and
`request_in_attempt` counts all pages and redirects within it.

Request ID, timing, HTTP status, capture date/digest, attempt number, request number
within the attempt, and rolling 1s/60s counts are on the left. Counts are sampled
at request start. Method and process ID follow, then variable-width fields:
thread, Retry-After, error type, request URL, and redirect Location. CSV quoting
preserves commas, quotes, and newlines within fields. Rows are appended in
completion order; sort by request ID to recover start order with multiple workers.
On orderly shutdown, unfinished requests get one row marked `Interrupted` with
blank duration and status. Totals and peak rates remain in the normal run log,
without adding non-request rows to the CSV.

Every run also writes `logs/<run>.429.jsonl` when the first 429 occurs, whether or
not CSV tracing is enabled. Each JSON line records the request ID, phase, URL,
status, UTC observation time, selected response headers (including Server, Date,
Content-Type, Retry-After, cache/request IDs, and Wayback markers), and a body
excerpt of at most 4096 decoded bytes. Header values are capped at 1024 characters.
Body collection reads at most 8193 wire bytes, with bounded gzip/deflate decoding;
the rejected response is then closed instead of draining the rest. Cookies and
authorization headers are excluded. Truncation and body-read failures are recorded;
diagnostic failures do not replace the 429 or change its retry classification.
CSV durations still end at response headers, before diagnostic body reads.

Repeated URLs with increasing `attempt` values are capture retries. A
`request_in_attempt` greater than 1 exposes redirect requests or CDX pagination. Capture
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

Collection configuration now uses `[collection]`, `[fetch]`, `[storage.local]`,
and optional `[storage.remote]`. See the [annotated workspace layout and complete
example](../../README.md#user-workspace). `storage.local.directory` is the working
root, resolved relative to the authored definition, outside its input directory.
It contains `data/`, `discovery/`, disposable `logs/`, and `.state/` receipts.
The lock lives beside `collection.toml`, surviving eviction of the working root.
`data/.staging/` holds recoverable annual transactions and is never uploaded.

Boto3 handles verified publication, restoration, and eviction. Explicit remote
reset still uses narrowly filtered rclone deletion. Credentials remain outside
configuration in the standard AWS credential chain. No component loads `.env`.
One process may own a local collection definition, and one writer may publish to
a bucket/prefix; cross-machine distributed locking is not implemented.

## Annual acquisition

Fetch visits selected years in order, taking the current UTC year once at run
start and skipping future years. CDX queries always cover January 1 00:00:00
through December 31 23:59:59. Exact configured/CLI dates filter playback captures
after acquisition; they do not narrow the CDX query.

For Wayback, complete historical years are cached as versioned JSON envelopes in
`discovery/wayback/v1/<query-hash>/YYYY.cdx.json` under the working root.
The envelope binds source, normalized query, year, and capture records. An existing valid cache avoids all CDX
requests for that year. Each record contains `urlkey`, `original_url`, `timestamp`,
`status_token`, `payload_digest`, and `mime`; an empty captures array represents a successfully
queried empty year. Fetch saves a cache only after every page and fallback slice
succeeds, writing a temporary sibling and atomically renaming it before playback.
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
7-day windows when smaller. Fallback slices run sequentially. For historical
years, `.state/discovery/wayback/v1/<query-hash>/YYYY/` retains the split plan and
each completed window, including successful empty windows. A restart follows
the saved plan, avoiding the failed annual query and completed windows. A window
is the checkpoint boundary: pages within an interrupted window are reacquired;
resume keys are not persisted. Checkpoints validate source, normalized query,
bounds, version, and capture records. Only complete calendar-year coverage
becomes the annual cache or starts playback. A terminal discovery failure retains
private progress, stops that year, and allows later years to finish, with a
nonzero final exit status. Current-year Wayback queries never reuse or create
discovery checkpoints.

Private discovery progress lives under the working root's `.state/discovery/`,
outside publishable caches. Completed caches are installed and synced before
their private progress is removed. Historical Wayback caches remain reusable
indefinitely, so later upstream additions require an intentional cache refresh.
Source, query, and format version select a namespace; changing queries cannot
silently reuse an incompatible listing. Completed Common Crawl units remain
reusable according to their crawl metadata freshness checks.

The CLI derives discovery from the working root. Programmatic settings may
supply `index_directory`; its default is `data_directory.parent / "discovery"`.
Completed validated cache files invoke the publication callback passed explicitly
in the discovery request immediately, including before a later WARC or discovery
unit fails. Missing
remote caches require explicit restore. Logs are never used as publication state.

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
write failure aborts the year but retains written records. Validated full
responses in the retained stage seed revisits on restart. Resetting one year
cannot invalidate another year's revisits.

Playback backpressure pauses all workers for 60, 120, 180 seconds and so on,
capped at ten minutes unless the source requests a longer wait. A failure after
the pause escalates even when retrying the same capture; failures received during
an active pause share its level and never shorten the pause. A successful
download after the pause resets escalation to the first step. A completion that
arrives during the pause does not reset it, and cooldown or idle time alone does
not count. Other acquisition failures neither pause the pool nor change the level.
Console messages report the applied cooldown and remaining pause; the request
CSV retains the raw `Retry-After` header, empty when absent, for diagnostics.
CDX discovery keeps its separate retry policy. Its query timeout excludes time
waiting at the shared gate. Timed-out or cancelled queries cannot start further
page requests; already in-flight requests finish under their socket timeouts.

Resolution and writing share an explicit annual worker batch. On failure or
Ctrl-C, it cancels queued groups, stops active groups before new captures,
attempts, or transport sends, and wakes retry and pacing waits. In-flight HTTP
operations finish under their existing timeouts. The batch drains before returning
or beginning the next year; acquisition staging is retained. Cancellation bypasses
source failure
classification; real backpressure already observed remains in force. Persistent
worker clients remain open across successful and failed batches until run cleanup.

For each year, Fetch opens or creates `data/.staging/YYYY/` on the same filesystem.
Its versioned `work.json` records archive/source/query/year/effective playback
dates, format version, original canonical file signatures, replacement mode,
and durable byte offsets per shard. Worker counts, pacing, retries, and shard
target may change on resume. The state is installed durably before acquisition.
Unchanged canonical WARC shards are hard-linked into the stage; the mutable tail
is copied before its first append. Resuming never copies over retained downloads.
Any copied or previously generated staged CDXJ is replaced by a fresh startup
index before it can supply the resume inventory.

Workers still buffer one complete URL result each, and bounded futures yield URL
results in order. The single writer appends each group's response/revisit records,
then flushes and `fsync`s every touched shard, syncs new directory entries, and
atomically replaces and syncs the work-state checkpoint. Only then does it log
URL completion. Errors and Ctrl-C retain acquiring stages. The remaining loss
window consists of downloaded URL results still buffered in workers or waiting
to be written; these may require downloading again.

Startup streams the working WARC view once, including its canonical baseline,
through a strict record iterator shared by recovery, indexing,
and restore. It explicitly verifies complete gzip members and CRCs, WARC framing,
record digests, and checkpoint boundaries; parser EOF alone does not establish
gzip completion. Each decoded member spills to temporary storage as needed and
stays available while the existing CDXJ indexer extracts fields and compressed
locators. Recovery and indexing share that spool without decompressing it again.
After recovery, Fetch syncs surviving recovered bytes and their checkpoint,
validates CDXJ ranges, and atomically installs the working index. Only then does
the CDXJ inventory reader build compact identities and response references and
allow discovery/playback. Partial or stale staged indexes cannot seed reuse.
Responses and revisits both prevent duplicate exact capture records; only full
responses with equal normalized WARC/CDX digests and an affirmative match flag
seed payload reuse. Representatives retain their actual HTTP status, with the
existing fallback for legacy CDXJ rows. Failed captures remain eligible for retry.
URL completion logs are never an inventory.

Only an incomplete final member in the highest private shard, beyond its durable
checkpoint, may be truncated automatically. Complete preceding records survive,
including records from an interrupted URL group, and are synced into the recovered
checkpoint. A newly created uncheckpointed shard containing only `warcinfo` is
removed. Missing checkpointed bytes, invalid complete records, canonical corruption,
or corruption in earlier shards fail explicitly and preserve files. A later write
or validation failure cannot roll back a previously checkpointed URL group.

The writer validates each serialized member before appending and returns changed
shard paths. It does not scan whole shards on close or rotation; final indexing
owns that validation and the verified record counts. Index preparation returns
scan results and temporary rows without installing them. The stage removes any
eligible empty tail, checkpoints recovered bytes, then installs the index after
range validation. Failed preparation or installation removes temporary indexes.

Finalization reindexes only shards written during the current invocation and
merges their entries into the verified startup index. With no appends, it reuses
that index without another decompression pass. Verified shard counts and sizes
are retained for artifact descriptions. The stage prepares one `YearChanges`
value containing artifact sizes, hashes and counts, changed files, and reset
deletions. It compares against the original canonical baseline, including
downloads retained from earlier invocations and index-only corrections. Local
promotion, remote generation receipts, and run logging reuse that same value.
After validating and syncing final artifacts, Fetch durably installs
`ready.json`, promotes changed WARCs, then promotes CDXJ. Indexing or publication
preparation failures retain acquisition for retry. A later fetch or manual sync
finishes interrupted ready promotions; unfinished acquiring stages remain private.
Unchanged years do not replace canonical files. An explicit full index rebuild
remains available to archive-wide reconciliation.

The default compressed WARC target is 250,000,000 bytes, a soft limit checked
between records. A URL group can span shards. Normal updates only
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
`discovery/common-crawl/v1/<query-hash>/<crawl-id>/<year>.json`, outside the resettable
data directory. They contain normalized query, full-year bounds, collection
metadata, and capture references including byte-range locators. Only complete
per-crawl queries are published atomically. A failed page or crawl prevents an
annual listing from reaching WARC work; already completed crawl caches survive.
Valid empty results are cached. Page counts precede date filtering, so a numbered
page's recognized no-captures 404 is also a valid empty result; other HTTP or
parsing errors still fail discovery. Corrupt entries fail explicitly, without silently
refetching or replacing them. Wayback uses its separate versioned envelope and query namespace.

Incomplete queries retain each successfully parsed numbered page, including
recognized empty pages, under
`.state/discovery/common-crawl/v1/<query-hash>/<crawl-id>/<year>/`. A restart obtains
a fresh page count before reusing those pages. Query, collection metadata, page
size, and page count must match; changed metadata or counts invalidate that unit's
private page progress. An interrupted page is reacquired. A complete crawl/year
cache is durably installed before private progress is cleared. Partial pages never
become a complete listing or start playback.

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
explicitly opted-in live check, copy the repository collection example, set
`fetch.source = "common-crawl"`, choose a small date range, and omit
`[storage.remote]` for local-only acquisition. Follow the smoke guide for any
separately authorized bucket operations.

## Publication

Fetch publishes each completed year before advancing. `--sync-only` retries
all local data and discovery without contacting upstream capture sources. It can
publish discovery alone when WARC acquisition failed. Data indexes are validated
against WARC byte ranges; WARCs upload before indexes. No ordinary operation
mirrors missing local files as remote deletions or prunes obsolete remote shards.
Sync-only finishes ready promotions and leaves acquiring stages untouched.
Working WARCs and private discovery checkpoints are never uploaded. An earlier
unfinished year does not prevent later completed years from publishing.

`BucketStorage` records remote signatures and local SHA-256 hashes in
`.state/publication.json`, bound to endpoint, region, bucket, prefix, and ID.
Initial adoption verifies content, rather than assuming multipart ETags are hashes.
Preflight refuses missing local baseline files and detects conflicting remote or
unrecorded local changes. Validated annual generations are recorded before local
promotion; intended upload hashes survive partial uploads and process failure.
Preflight reconciles completed uploads before retrying outstanding publication.

`--restore` downloads managed data and discovery into temporary staging, validates
cache provenance and replay indexes, refuses differing local files, checks remote
stability, and installs complete files. `--evict-local` checks content for every
local finalized data/cache object before removing output. It refuses pending
transactions, outstanding WARC acquisition or discovery progress, unknown files,
and missing/different remote copies. Neither operation
changes collection definitions, assets, or published objects.

`--publish-metadata` validates `[collection]`, uploads its referenced assets, then
publishes generated `archive.json`. Name and homepage are required only for this
operation. Fetch validates the manifest independently of Navigator; contract tests
verify their agreement. Normal data publication never publishes presentation edits.

## Reset and migration

Local `--reset-data` rebuilds selected years through staging. Remote
`--reset-data` remains a destructive full-range operation: it rejects date
overrides, warns of playback downtime, deletes only managed files for this archive
under remote `data/`, preserving metadata, assets and unrelated objects,
clears the local data directory, and rebuilds and publishes years in order.
Explicit reset discards selected unfinished WARC stages under the same local or
remote scope. Both reset modes preserve compatible discovery caches/checkpoints
and reuse them to rebuild WARC contents. If replacement is interrupted, a subsequent
ordinary run resumes the saved replacement mode. A successful empty selection can
clear a local year through reset staging. WARC failures also leave discovery intact.

Incompatible acquisition settings or a changed canonical baseline fail with the
stage preserved; restore matching settings or explicitly reset. Legacy unfinished
stages without resume metadata are preserved and rejected, rather than silently
deleted. Existing readiness manifests remain recoverable.

After local cleanup, run explicit `--restore` before resuming acquisition.
Migration of the local workspace is offline and separate from bucket operations;
see the migration guide for copy/verify/remove behavior.

For existing flat bucket layouts, follow the [migration guide](../../docs/BUCKET-CATALOG-MIGRATION.md).
Fetch does not migrate or delete old root objects automatically.

## Code organization

Fetch is organized by pipeline stage. Workflow modules expose one primary
operation near the top, followed by private helpers. Shared records, cohesive
stateful classes, and reusable primitives may expose the related operations they
need. Internal Python import paths are not a compatibility interface; the CLI,
configuration and cache formats change as documented; the published replay format remains unchanged.

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
      checkpoints.py                manage private discovery progress
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
    stage_year.py                   resume, checkpoint, promote, and recover a year
    publication/
      sync_archive.py               publish committed artifacts
      purge_remote.py               explicitly reset managed data
      run_rclone.py                 configure and invoke rclone
  archive/
    inventory_collection.py        read stored captures and reusable responses
    scan_warcs.py                   shared strict record iterator and tail recovery
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
separate despite both using CDX terminology. Each year starts with recovery and
CDXJ generation, followed by inventory loading. Acquisition then follows these
boundaries:

```text
Discover -> Resolve -> Write -> final Index -> Commit -> Publish
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

- `discover(request, stats)` returns a complete `CaptureListing` and query metadata;
  its optional `request.on_cache_complete(path)` callback publishes each completed
  cache immediately. Wayback discovery installs the run's shared transport instrumentation.
- `open_client(stats)` is a context manager creating one persistent worker client;
  the source explicitly installs transport instrumentation before yielding it.
- `fetch(client, capture)` performs one retrieval-and-decoding attempt and returns
  a normalized `CaptureResult`. It receives the complete `CaptureRef`, not just
  its identity.
- `preflight(capture)` may return a failure without starting an attempt.
- `failure_advice(error, attempt)` supplies a neutral category, retry decision,
  delay, coordinated cooldown, and optional failure-group limit.
- `capture_link(capture)` receives the complete `CaptureRef` and supplies the source URL used in terminal links. Capture outcomes carry that reference through reporting.

The runner receives the publisher explicitly, and discovery depends only on its
completion callback. Publication does not depend on ambient context variables.
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
run-record fields remain unchanged; the request trace adds a `phase` column. CC query metadata in
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
verified bucket publication. Indexing tests compare library fields, sorting, and
compressed locators across shards; startup tests assert one decompression pass,
rejection of failed or partial indexes, index-only corrections, and no second
indexing scan when a resumed run appends nothing. Inventory tests cover exact
identities, digest/flag restrictions, actual HTTP status and legacy fallback,
empty redirects, scope, and earlier-response restrictions. Navigator exercises
replay of recovered responses and revisits across shards. Run the suites separately:

```console
.venv/bin/python -m pytest archive-magic-fetch/tests -q
.venv/bin/python -m pytest archive-magic-navigator/tests -q
```
