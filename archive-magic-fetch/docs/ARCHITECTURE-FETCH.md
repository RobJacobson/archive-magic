# Archive Magic Fetch architecture

Archive Magic Fetch builds annual, size-bounded WARC 1.1 collections and CDXJ
indexes from Internet Archive history for one website pattern. This document is
the authoritative description of Fetch.

## Command and output

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

The default range is 1995 through the current UTC time. Exit status is zero only
when every selected capture is represented and publication succeeds. A partial
run keeps finalized WARCs and indexes usable, records unresolved captures in
`failures.json`, and returns nonzero.

```text
<archives-root>/example.org/
├── collection.json
├── failures.json
├── index.cdxj
├── archive/
│   └── 2004/
│       ├── example.org-2004-001.warc.gz
│       ├── example.org-2004-002.warc.gz
│       └── example.org-2004.cdxj
└── sources/<UTC-run-id>/
    ├── query.json
    └── 2004.page001.cdx
```

CDXJ filenames are collection-relative POSIX paths. Finalized WARCs are
immutable. Temporary files are cleaned on startup, and collection metadata is
published atomically. Collection schema version 3 has no compatibility contract
with older Fetch output; regenerate older collections.

## Annual processing

Years are processed serially in ascending order. Each annual WARC set and its
annual CDXJ are independently replayable: revisits may cross WARC shards within
the year but never refer to another year.

For each year Fetch:

1. Downloads and durably saves raw CDX entity bytes before parsing them.
2. Parses, validates, de-duplicates exact CDX rows, and orders them by timestamp.
3. Inventories only the year's finalized WARCs.
4. Processes each capture synchronously:
   - skip an exact identity already represented in the year;
   - write a revisit when an earlier matched response has the same URL key and
     valid CDX payload digest;
   - otherwise download the exact capture and write a full response; or
   - record an unresolved failure and continue.
5. Finalizes size-bounded WARC shards, publishes the annual index, then merges
   the collection index and checkpoints the manifest and failure ledger.

A failed capture does not establish reuse. A later capture with the same key
gets its own download opportunity. A response establishes reuse only after its
WARC write succeeds.

Redirects and captures without valid CDX digests always download individually.
Redirect bodies may be empty and their `Location` headers are preserved. Fetch
requests exact timestamps and URLs, uses original/raw playback, and never follows
historical redirects or substitutes a nearest capture.

## Payload identity and imperfect playback

Capture identity preserves URL key, original URL, raw timestamp, raw CDX status,
and raw CDX digest. WARC extension headers retain these CDX values separately
from `WARC-Payload-Digest`, which describes the bytes actually stored.

Payload reuse is annual and same-URL only, keyed by `(urlkey, valid CDX digest)`.
The first successfully written, digest-matched response becomes the representative
for later captures in that year. Revisits reference its local payload digest.

When IA serves usable bytes that disagree with the CDX digest, Fetch keeps the
response and writes `CDX-Digest-Match: false`. That response represents its exact
capture but never becomes a reuse representative, so later captures can obtain
better data. Empty non-redirect bodies and known IA stubs remain failures.

## Network and retry policy

Playback uses one persistent client and one request at a time. There is no
proactive pacing, concurrency controller, adaptive rate policy, or global
cooldown.

Transport failures, HTTP 5xx, and HTTP 429 receive at most three total attempts.
The two retry delays are 5 and 10 seconds. A positive `Retry-After` value replaces
the corresponding delay. Exact mismatches, blocked captures, unusable bodies,
truncated stored payloads, and other permanent failures continue immediately.

CDX acquisition is also serial and retains its separate reactive retry loop.
HTTP 429 honors `Retry-After`; transient connection and server failures use
bounded delays. Fetch relies on observed serial behavior before adding any new
rate-control mechanism.

## Recovery, indexing, and validation

WARC shards target 1 GB compressed size and use sequences `001` through `999`.
A shard is written to an exclusive temporary path, closed, validated, and
atomically published. A crash may discard the current unpublished shard, but a
finalized shard is never replaced. Startup reconciliation indexes finalized
WARCs that missed publication before the crash.

Annual index validation requires every locator to address an immutable WARC
range in that year and every revisit to resolve to a same-year full response at
an equal or earlier timestamp. The collection index is a sorted merge of annual
indexes; it does not change annual dependency boundaries.

The manifest keeps outcome counts and only operational metrics that remain
meaningful for serial work: CDX requests and duration, playback attempts and
bytes, WARC/index timing, and attempts by failure category.

Interactive console output numbers every selected capture as `current/total`.
Capture labels use OSC 8 hyperlinks so terminals can display the compact
`timestamp/original-url` form while opening the full Wayback URL. Successful
downloads, revisits, warnings, and errors use distinct ANSI styles. Redirected
output remains plain text, and the `NO_COLOR` convention disables color.

## Deliberate exclusions

Fetch has no thread pool, process pool, durable job queue, database, cross-year
payload reuse, cross-URL digest reuse, loose-file output, redirect expansion,
link rewriting, cloud-storage abstraction, or compatibility layer for older
collections. Multiprocessing by year is a future measurement-driven feature,
not latent infrastructure.
