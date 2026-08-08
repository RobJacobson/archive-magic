# Archive Magic Fetch clean-sheet rewrite handoff

## Status and authority

This is the authoritative implementation handoff for the clean-sheet rewrite
of `archive-magic-fetch`. It replaces the earlier audit-oriented contents of
this file and supersedes the current Fetch architecture document wherever the
two disagree.

The user has approved the decisions below. Do not reopen them casually, add
compatibility behavior for the implementation being replaced, or preserve old
features merely because code and tests for them already exist.

At the time this handoff was written:

- the active rewrite branch is `refactor-fetch-code`;
- `main` retains the implementation being replaced and may be consulted with
  `git show main:<path>`;
- `refactor-fetch-code` and `main` currently point at the same commit, so this
  handoff must be committed before the old implementation is deleted from the
  rewrite branch;
- existing collections contain test data only and will be deleted by the
  user; and
- no old collection layout, manifest, CLI option, or output needs migration or
  backward compatibility.

The desired result is a replacement, not a parallel `v2`. Delete the existing
Fetch implementation and obsolete tests on the rewrite branch. Keep the same
package and command name. Refer to `main` when a proven low-level detail is
useful, but do not preserve the old module structure by default. If visual
side-by-side inspection is necessary, use Git or a separate worktree, never a
`legacy/`, `v1/`, or duplicate package inside the rewrite tree.

## Objective

Archive Magic Fetch has one primary job:

1. Query Internet Archive CDX metadata using the `wayback` library.
2. Determine which exact captures require Internet Archive playback.
3. Download only those captures through a polite, bounded fetch queue.
4. Write the selected history into annual, size-bounded WARC 1.1 files.
5. Build annual and collection-wide CDXJ indexes for pywb playback.

The governing principles are KISS, YAGNI, and DRY. The current implementation
is approximately 5,529 production lines and 6,143 test lines. A useful rewrite
guardrail is fewer than roughly 2,750 production lines, but line count is not
a substitute for clear ownership and correctness. Prefer a small number of
cohesive modules and one obvious path through the pipeline.

## Core invariants

The rewrite must preserve these invariants:

- A selected capture is either represented in the collection or reported as
  an unresolved failure.
- No capture is downloaded when the exact capture is already available in the
  current collection.
- Same-URL payload reuse never invents redirect headers or silently substitutes
  a different capture.
- Requests are exact: no nearest-capture fallback and no followed redirects.
- Every published CDXJ locator points to an immutable, finalized WARC byte
  range.
- Annual WARCs are organizational and recovery partitions, not independently
  portable packages. Collection playback requires the collection CDXJ plus
  every WARC referenced by revisits.
- Every revisit resolves to a full response in the same year or an earlier
  year (backward-only).
- A crash may require repeatable local indexing or a bounded amount of network
  refetching from an unfinalized WARC, but it must never corrupt or replace a
  previously published WARC or index.
- Collection metadata never describes a partial run as complete.

## Features deliberately removed

The following are out of scope and must not survive as dormant code, CLI
aliases, deprecation shims, empty folders, compatibility branches, or copied
tests:

- loose `website/` file output;
- `--files latest|unique|all`;
- `--rewrite-local` and local-link rewriting;
- `--build-warc` or a mode that performs CDX search without the core WARC
  output;
- redirect-report generation and a `redirects.json` artifact;
- automatic redirect expansion or following redirect targets;
- old coverage-window merging and optimization based on a previously queried
  date envelope;
- old per-resource WARC allocation and path collision machinery;
- one-WARC-per-resource output;
- persistent per-WARC/shard CDXJ files;
- cross-URL payload-digest deduplication;
- a generalized database, Redis, or durable job queue;
- a generalized cloud-storage provider layer;
- R2 uploads or downloads in this rewrite;
- old collection migration or compatibility; and
- broad test matrices for deleted flags, path spellings, formatting helpers,
  or implementation details.

Redirect captures themselves remain ordinary historical captures. Store their
actual 3xx status and `Location` header in WARC response records, do not follow
them, and do not deduplicate them through payload digests.

Loose files can later be implemented as a separate extractor that reads the
finished WARC/CDXJ collection. They do not belong in the fetch pipeline.

## Minimal command-line contract

Keep the command name:

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

Retain the current stable default output-root convention unless a repository
integration requirement proves that one small output option is necessary.
The initial rewrite should not expose rate, concurrency, retry, WARC size,
files, rewrite, storage-provider, or index-publication policy as CLI options.
Use named constants for policies that may need tuning after measurement.

The accepted URL-pattern scope remains one website collection, including the
currently documented exact host, path-prefix, and `*.example.com` forms. Keep
normalization necessary to derive a safe, stable collection ID and canonical
CDX query. Do not restore the old readable-per-URL WARC path allocation logic.

Defaults remain the beginning of practical Wayback history (1995) through the
current UTC time. Interpret year boundaries in UTC. Reject an invalid or
reversed range before any network or filesystem mutation.

Return behavior:

- `0`: all selected captures were represented and all required publications
  succeeded;
- nonzero: a fatal error or one or more unresolved captures occurred;
- successfully finalized WARCs and indexes remain usable after a partial run;
  and
- the manifest and failure artifact distinguish partial success from complete
  success.

## Collection layout

Use this permanent local layout:

```text
<archives-root>/
└── example.org/
    ├── collection.json
    ├── failures.json                 # only when unresolved failures exist
    ├── archive/
    │   ├── 2004/
    │   │   ├── example.org-2004-001.warc.gz
    │   │   ├── example.org-2004-002.warc.gz
    │   │   └── example.org-2004-003.warc.gz
    │   └── 2005/
    │       └── example.org-2005-001.warc.gz
    ├── indexes/
    │   ├── years/
    │   │   ├── 2004.cdxj
    │   │   └── 2005.cdxj
    │   └── index.cdxj
    └── sources/
        └── <UTC-run-id>/
            ├── query.json
            ├── 2004.cdx
            └── 2005.cdx
```

The exact raw-CDX extension may be `.cdx.gz` when the preserved response body
is compressed. Do not decompress and then claim that the file is byte-exact.
Record the encoding and checksum in `query.json`.

R2 uses flat object keys with slash-delimited prefixes, so every permanent
path must also be a valid environment-independent object key. CDXJ `filename`
values are collection-relative POSIX paths such as:

```json
{"filename":"archive/2004/example.org-2004-003.warc.gz"}
```

Never store absolute paths in CDXJ or `collection.json`.

Temporary WARC and CDXJ files must live outside the visible permanent layout,
or use unmistakable temporary names that are ignored and cleaned on startup.
They are implementation artifacts, not collection objects.

## Year partitioning and work order

Partition both CDX acquisition and WARC publication by capture year. Process
years in ascending order. A partial `--start` or `--end` year uses the exact
requested boundary, not the whole calendar year.

Within a year:

1. Parse and validate CDX rows.
2. Group non-redirect captures by normalized URL and valid CDX payload digest.
3. Order candidate captures within a group by timestamp.
4. Prefer exact local captures before scheduling network work.
5. Schedule one representative candidate for each URL/digest group.
6. If the representative fails, schedule the next candidate in that group.
7. After a representative succeeds, write later matching captures as revisits.
8. Schedule every redirect and every capture without a usable digest
   individually.

Give ready first-attempt jobs a deterministic priority of:

```text
(capture timestamp, canonical URL, stable capture identity)
```

Retries enter a separate delayed queue and do not jump ahead of ready first
attempts. This scheduling order is deterministic; physical WARC record order
need not be.

Do not sort WARC payloads after download. Parallel requests finish out of
order, and enforcing physical order would require unbounded memory, a second
spooling phase, or head-of-line blocking behind a slow request. The CDXJ, not
WARC byte order, supplies playback ordering.

## CDX acquisition and raw preservation

Continue using `wayback` as the supported Internet Archive client boundary.
Do not replace it wholesale merely to avoid a narrow response hook. Direct
calls to the documented public CDX endpoint are acceptable only when required
to preserve the unmodified response body and should reuse the same session,
user agent, timeout, rate-limit, and error handling policy.

For every annual query:

1. Create the run source directory before parsing results.
2. Save the exact response entity bytes before `wayback.CdxRecord` or any local
   parser normalizes fields.
3. Record query URL/parameters, requested bounds, response encoding, byte
   length, checksum, retrieval time, and client version in `query.json`.
4. Parse from the saved bytes so the durable source and processed input cannot
   diverge.
5. Preserve malformed rows in the source and add a deterministic failure entry
   rather than silently dropping them.

The installed `wayback` library is known to repair invalid month/day `00`
timestamps, remove redundant ports, convert tokens to typed values, and skip
some malformed URLs. The saved source must precede those transformations.

One annual partition is the basic CDX retry/resume boundary. Do not add a
database just to checkpoint searches. If a representative annual query is too
large or repeatedly fails late, use deterministic documented CDX pagination
or smaller date partitions within that year, save every raw page, and collapse
overlapping boundary rows by stable capture identity.

The CDX search limit is separate from playback. Preserve the `wayback`
library's shared default CDX pacing unless current official guidance requires
a lower limit.

## Stable capture identity

Define one capture identity in one module and use it for CDX deduplication,
existing-WARC inventory, failure accounting, and publication validation.

At minimum, identity must preserve enough raw information to distinguish:

- canonical CDX URL key;
- original URL spelling when IA returns distinct rows for it;
- raw 14-digit capture timestamp;
- raw CDX status token, including the `-`/unknown sentinel; and
- raw normalized CDX payload digest, including an explicit missing sentinel.

Identical duplicate CDX rows collapse to one logical capture. Known distinct
statuses at the same URL and timestamp remain distinct.

The statusless-row bug must be resolved explicitly:

- persist the original CDX status token on every generated response/revisit in
  a small WARC extension header;
- use that extension header, not the numeric HTTP status alone, when rebuilding
  identity from a WARC;
- allow the actual archived HTTP block to retain the numeric status returned by
  exact playback; and
- prove on second and third identical runs that no network request or duplicate
  revisit is created.

Similarly, retain the distinction between:

- IA's CDX payload digest, stored in a WARC extension header; and
- the digest of the actual stored WARC payload, stored as the standard
  `WARC-Payload-Digest` and used for validation and revisit references.

Do not silently drop status, timestamp, or digest from identity to make a test
pass.

## Exact playback and validation

Playback requests remain exact-only:

- original/raw mode;
- `exact=True`;
- `follow_redirects=False`;
- no nearest capture; and
- no automatic expansion to redirect targets.

For a CDX row with known values, validate returned URL, timestamp, and status
against that row. For a statusless CDX row, accept the returned numeric HTTP
status but preserve the original unknown status token separately in identity.

A response with a mismatched timestamp, URL, or known status is not the
requested capture. Do not store it as though it were exact. Retry only when the
failure category is plausibly transient, then report it.

Redirects require individual playback because the CDX payload digest does not
validate `Location` or the rest of the response headers.

## Existing-content reuse and annual recovery boundaries

Every run inventories the current-format collection before scheduling network
work. Existing exact captures are reused regardless of which annual shard
contains them. Finalized WARC objects are immutable; do not rewrite them merely
to sort records or consolidate free space.

Years are CDX query, checkpoint, WARC naming, and annual-index boundaries—not
deduplication boundaries:

- build a collection-wide representative map keyed by `(urlkey, IA/CDX digest)`
  from finalized WARCs (compact locator metadata only; never payload bytes);
- store one full response for the oldest successful capture of that key;
- write same-key later captures as WARC revisits that may refer to a response
  in an earlier shard of the same year or an earlier year;
- never create a revisit that points forward in time; and
- validate that every annual revisit resolves against the current year or an
  earlier year in the collection chain.

If a logo or stylesheet keeps the same IA digest across years, later years emit
revisits rather than re-downloading bytes. Earlier unresolved failures stay in
`failures.json` even if a later year obtains the payload—backward-only reuse
never repairs the past.

On restart, discard unpublished partial shards, reconcile finalized-but-
unindexed WARCs, rebuild the compact map from on-disk responses, and resume the
requested year range so `2016–present` can reuse finalized `2000–2015` assets.

Cross-URL digest deduplication remains out of scope. Prior measurement found a
maximum additional saving below 1% for the representative large collection,
while it complicates dependency closure.

## Fetch scheduler and rate policy

Separate request-start rate from in-flight concurrency.

The current `wayback` defaults are process-wide and shared across sessions:

- Memento/playback starts: 8 per second;
- CDX search starts: 0.4 per second.

Use smooth playback pacing, approximately one new start every 125 ms. Do not
burst eight requests on a wall-clock second boundary and do not implement a
sliding timestamp list when the library's shared `RateLimit` already provides
the necessary gate. There must be exactly one owner of normal pacing.

Use a separately bounded in-flight limit. Benchmark 16, 24, and 32 on a
representative subset and choose the smallest value that sustains the allowed
start rate without excessive memory or open connections. Keep it as a named
constant initially rather than a CLI option.

The scheduler needs only:

- a deterministic ready queue;
- a delayed retry priority queue keyed by monotonic eligibility time;
- a bounded set of in-flight requests;
- a collection-wide `blocked_until` monotonic deadline for 429 responses; and
- bounded handoff/backpressure between completed downloads and the WARC writer.

Workers never sleep while owning an in-flight slot. A retryable result returns
the slot and enters the delayed queue.

On HTTP 429:

- parse and honor `Retry-After`;
- close the global start gate for all requests;
- use a conservative default cooldown when the header is absent;
- increase the cooldown after repeated 429s; and
- resume smooth pacing after the deadline.

On transient connection errors and retryable 5xx responses, back off the
individual job rather than stopping all starts, unless evidence shows a broad
service outage. Cap retry delay and total attempts with named constants so a
single capture cannot wait for days. Permanent policy, malformed-response,
exact-mismatch, decoding, and validation failures do not retry indefinitely.

Use monotonic time for scheduling. Respect cancellation/fatal errors promptly.
Aggregate telemetry is useful; per-request durable event logs are not.

## WARC construction

Write WARC 1.1 using `warcio`. Pywb supports playback of WARC 1.1. Each file
contains a valid `warcinfo` record and uses a correct `WARC-Filename` basename.

Use one named size constant with a default target of 1 GB. Measure the
compressed `.warc.gz` file size. Rotate after completing a record once the
target has been reached; never split a WARC record merely to meet the target.
A single unusually large record may therefore make a shard exceed 1 GB.

Names use exactly three sequence digits, starting at `001`:

```text
example.org-2004-001.warc.gz
example.org-2004-002.warc.gz
...
example.org-2004-999.warc.gz
```

If another shard would require `1000`, fail loudly before creating it. The
approved assumption is that a website exceeding approximately 1 TB in one
year indicates a scope or data problem requiring operator review.

Use one WARC writer owner. Download workers return validated results through a
bounded queue; they never write concurrently to the open WARC. Physical record
order follows bounded completion flow, not a post-download sort.

Write to an exclusive temporary path. Finalization must close the gzip stream,
validate the WARC, flush durable local state as appropriate, and atomically
publish the final path. A finalized WARC is immutable.

If a run appends newly discovered captures to an already indexed year, use the
next sequence number. Do not repack old shards to restore chronology.

## CDXJ construction

Maintain exactly two permanent index granularities:

1. `indexes/years/YYYY.cdxj`: all published WARCs required to replay that
   annual set.
2. `indexes/index.cdxj`: the whole collection.

There are no permanent per-WARC or shard indexes.

After each WARC closes:

1. Generate a sorted CDXJ fragment for that WARC in a temporary location.
2. Merge it with the existing sorted annual CDXJ into a new temporary annual
   CDXJ.
3. Remove exact duplicate index entries deterministically if recovery repeats
   the merge.
4. Validate filename, offset, and length against every referenced finalized
   WARC.
5. Validate backward revisit dependency closure (current or earlier years).
6. Atomically replace the annual CDXJ.
7. Delete the temporary fragment.

If a crash publishes a WARC before its annual-index merge, resume compares the
year's finalized WARC filenames with filenames represented in the annual CDXJ
and indexes only missing WARCs. Losing a temporary fragment costs at most a
repeatable scan of the newly finalized WARC; it never loses downloaded data.

After each year completes, merge all current annual indexes into a sorted
temporary collection index and atomically replace `indexes/index.cdxj`. Repeat
once at successful final completion. Never append annual index text directly
to the global file: CDXJ must be globally ordered by URL key and timestamp.

Index response and revisit records used for replay. Keep indexes plain and
uncompressed initially so pywb can binary-search them efficiently. ZipNum or
another sharded query index is a future scaling decision, not part of this
rewrite.

## Publication, manifest, and failures

`collection.json` is the small authoritative publication manifest, not a claim
that a date envelope was queried successfully. Keep its schema minimal and
versioned. It should identify at least:

- collection/schema version;
- normalized collection ID and requested URL pattern;
- WARC version and configured size target;
- every finalized WARC's relative key, year, byte size, checksum, and record
  count;
- every annual index's relative key, byte size, checksum, and capture count;
- the collection index key, size, checksum, and capture count when present;
- the current run source/query directory;
- counts of selected, represented, locally reused, downloaded, revisited, and
  unresolved captures; and
- collection status: `complete` or `partial`.

Do not use the manifest to skip future CDX queries merely because a year or
date range was queried before. Current IA metadata is re-queried for the
requested run, then exact existing captures suppress network playback.

`failures.json` exists only when unresolved failures remain. It is a compact,
versioned, deterministically sorted final-state ledger keyed by capture
identity. Distinguish at least:

- malformed raw CDX;
- blocked/policy denial;
- exact URL/timestamp/status mismatch;
- unavailable playback;
- retry exhaustion by HTTP or connection category;
- truncated/corrupt response;
- digest ambiguity or validation failure; and
- publication/index validation failure where capture scope is known.

Do not duplicate console prose or create a durable per-attempt event log. A
later run that successfully represents a previously failed capture removes it
from the current unresolved ledger.

Publication order for a normal shard is:

1. finalized immutable WARC;
2. atomically updated annual CDXJ;
3. updated manifest describing the partial or complete current state;
4. collection CDXJ after the year completes; and
5. final manifest/failure publication.

A fatal error before a publication boundary leaves the previous published
state valid. Startup removes abandoned temporary files and reconciles any
finalized WARC missing from its annual index.

## R2 future compatibility

R2 will eventually be the single source of truth for WARC and CDXJ objects,
while the collection CDXJ will be cached locally for query performance. Do not
implement R2 in this rewrite.

Design local publication so the later mapping is direct:

- local relative path equals future R2 object key;
- WARC and versioned data objects are treated as immutable;
- CDXJ locators remain collection-relative;
- publication logic is concentrated in a small filesystem boundary; and
- no code assumes all WARC bytes must be downloaded for playback.

R2 supports ranged reads, so future playback can translate CDXJ filename,
offset, and length into one object-range request. The future local cache needs
only `collection.json` and `indexes/index.cdxj` for whole-collection querying;
annual indexes are downloaded only for annual-package work.

Do not invent a provider interface with multiple hypothetical backends. A
small cohesive local publication component is enough preparation.

## Navigator change required in this branch

Navigator currently expects:

```text
replay/index.cdxj
```

Change it to:

```text
indexes/index.cdxj
```

Update collection discovery/validation, pywb configuration, fixtures,
integration tests, README material, and architecture documentation that refer
to the old path. Navigator continues resolving CDXJ `filename` beneath the
collection root for this local-only rewrite.

Retain one real pywb integration test proving that Navigator can replay WARC
1.1 responses and revisits when the full response and revisit occupy different
WARC files in the same year.

## Suggested code shape

Prefer approximately these responsibilities, combining modules further when
the result remains clearer:

- `cli.py`: minimal argument parsing and exit status;
- `models.py`: small immutable capture/job/result types;
- `cdx.py`: annual query, raw persistence, parsing, identity input;
- `scheduler.py`: ready/delayed queues, shared rate gate, concurrency, 429;
- `warc.py`: existing inventory, reuse, WARC 1.1 response/revisit writing and
  rollover;
- `index.py`: temporary WARC indexing, annual merge, collection merge;
- `collection.py`: paths, atomic publication, manifest, failure ledger;
- `fetch.py`: the short orchestration pipeline.

Do not create layers merely to match this list. Avoid manager/factory/service
class proliferation. Prefer functions and small immutable records. One state
machine for the scheduler and one writer owner are enough.

Review every retained dependency after the rewrite. Keep `wayback`, `warcio`,
and `cdxj-indexer` when used. Remove old dependencies and helpers that no
longer serve the core path.

## Test policy

There is no line-coverage target. Tests must protect likely expensive or silent
failures: data loss, duplicate playback, rate-limit abuse, corrupt publication,
or broken replay. Delete the old unit suite rather than making the new design
simulate old internals.

Required high-value tests are:

1. Raw CDX bytes are saved before normalization; a malformed row remains in
   source and appears in `failures.json`.
2. A statusless CDX capture run three times produces one logical capture and no
   network request after the first successful run.
3. A fake-monotonic-clock scheduler proves starts are smoothly spaced, in-flight
   concurrency is separate, delayed retries release slots, and one 429 closes
   the global gate through `Retry-After`.
4. An exact existing-WARC capture prevents a network call.
5. Same URL/digest captures require one successful representative playback and
   produce valid revisits (including later years); redirects still fetch
   individually.
6. Matching payloads in later years become backward revisits, not full annual
   duplicates; earlier failures are not repaired by later successes.
7. WARC rollover uses WARC 1.1, three-digit names, the configured compressed
   size target, and rejects a required shard `1000`.
8. Crash recovery indexes a finalized-but-unindexed WARC without downloading
   it again and does not expose an incomplete WARC.
9. Annual-index incremental merge and collection-index merge remain globally
   sorted, reference valid ranges, and are idempotent.
10. Real pywb/Navigator integration replays a response in `001` through a
    revisit in a later same-year WARC and a response in one year through a
    revisit in a later year.
11. A partial run publishes usable successes, a truthful partial manifest, a
    deterministic failure ledger, and a nonzero exit.

Use tiny fixture WARCs and fake network responses. Do not add exhaustive tests
for every path character, every console sentence, every dataclass field, or
third-party library behavior already covered upstream. Add another test only
when it protects a demonstrated bug or a high-consequence invariant.

## Aggregate observability

Keep machine-readable aggregate run metrics in the manifest or `query.json`:

- CDX request count and duration;
- playback starts/completions, bytes, and peak in-flight count;
- time waiting for rate gate and global 429 cooldown;
- attempts and scheduled delay by broad failure category;
- exact local reuse and digest/revisit savings;
- WARC write/finalization time;
- annual and collection index time; and
- total unresolved failures.

Do not emit a durable line per successful capture. Human console output should
show phase progress, finalized WARC/year boundaries, aggregate rates, and final
status.

## Implementation sequence

Recommended order:

1. Commit this handoff on `refactor-fetch-code`.
2. Delete the old Fetch modules, old Fetch tests, and obsolete Fetch
   architecture text on the rewrite branch. Leave `main` untouched as the
   reference.
3. Build the collection paths, manifest skeleton, raw annual CDX persistence,
   and stable capture identity.
4. Build a synchronous exact-download-to-one-WARC vertical slice.
5. Add WARC 1.1 rollover and annual CDXJ publication.
6. Add existing-capture inventory and same-year representative/revisit reuse.
7. Add the central scheduler, bounded concurrency, delayed retries, and global
   429 gate.
8. Add multi-year collection-index merge, partial-failure publication, and
   crash reconciliation.
9. Update Navigator to `indexes/index.cdxj` and pass the real pywb integration
   test.
10. Benchmark concurrency, inspect production/test line counts, remove unused
    abstractions and dependencies, and rewrite the architecture documentation
    to describe only the finished implementation.

Keep the system runnable after each vertical milestone where practical. Do not
restore deleted features to make an old test pass.

## Definition of done

The rewrite is complete only when:

- the CLI performs the five-stage core workflow without deprecated modes;
- raw annual CDX is durably preserved before normalization;
- malformed and statusless CDX rows are handled deterministically;
- exact existing captures prevent playback requests;
- the scheduler sustains polite smooth starts with bounded memory and a global
  429 cooldown;
- WARC 1.1 files roll at the named 1 GB target and use `001`-`999` names;
- every annual set is self-contained even when it contains multiple WARCs;
- annual CDXJ is updated after each WARC without permanent shard indexes;
- collection CDXJ is globally sorted and atomically published;
- partial success is usable, truthfully described, and returns nonzero;
- Navigator reads `indexes/index.cdxj` and real pywb playback passes;
- no legacy Fetch implementation or parallel `v2` remains in the tree;
- no loose-file, rewrite, redirect-report, coverage-merge, migration, or R2
  feature has crept back in;
- the focused high-value tests pass; and
- documentation describes the new system rather than the deleted one.

If an implementation detail is not specified here, choose the option with the
fewest persistent concepts, fewest public knobs, and clearest failure mode.
