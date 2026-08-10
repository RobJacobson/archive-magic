# Archive Magic Fetch architecture

Archive Magic Fetch builds immutable, portable WARC 1.1 collections and sorted
CDXJ indexes from Internet Archive history for one domain pattern. The current
CLI partitions captures by UTC calendar year, but collection publication is
identified by a generic filesystem-safe collection ID.

## Command and output

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

The default range is 1995 through the current UTC time. Expected capture-level
failures do not fail the command; they are reported in the corresponding
immutable run record. Unexpected exceptions return nonzero.

```text
<archives-root>/example.org/
├── collections/
│   └── 2004/
│       ├── example.org-2004-001.warc.gz
│       ├── example.org-2004-002.warc.gz
│       └── example.org-2004-index.cdxj
└── captures/
    └── 2004/
        └── runs/<UTC-run-id>/
            ├── run.json
            └── page-001.cdx.gz
```

Only `collections/**` is required for replay or bucket publication.
`captures/**` is acquisition provenance and operational diagnostics. Fetch does
not publish a domain-wide merged index, catalog, mutable failure ledger, or
collection manifest.

Each CDXJ stores a WARC basename such as
`example.org-2004-001.warc.gz`. The index and its WARCs can therefore be copied
together to another directory and replayed by configuring that directory as
pywb's archive path.

## Collection processing

Years are selected serially in ascending order. For each selected year Fetch:

1. Creates `captures/<year>/runs/<run-id>/` and durably saves every raw CDX HTTP
   entity as `page-NNN.cdx.gz` before parsing it.
2. Parses, validates, de-duplicates exact CDX rows, and orders them by timestamp.
3. Inventories only finalized WARCs in `collections/<year>/`.
4. Skips identities already represented; writes a same-collection revisit when
   an earlier matched response has the same URL key and valid CDX digest;
   otherwise attempts exact playback and records any failure for this run.
5. Finalizes size-bounded WARC shards, builds and validates the collection CDXJ,
   and atomically publishes it.
6. Atomically publishes `run.json` last. Its presence marks normal completion
   for that collection's slice of the invocation.

One invocation ID is shared by all selected yearly collections. A year with no
playable records still gets its capture run record, but it does not get a
`collections/<year>/` directory or empty replay index.

`run.json` contains the archive and collection IDs, bounded dates, URL pattern,
query/page metadata, outcome counts, operational timings, current-run failures,
and a complete post-run WARC/index artifact snapshot with hashes and sizes.
Run records are immutable history; Fetch does not carry failures forward.

## Recovery, identity, and validation

Finalized WARCs are the recovery source of truth. On a rerun, Fetch requeries
CDX, inventories existing WARCs, skips represented identities, and retries
missing captures. Startup reconciliation rebuilds a collection index when a
crash finalized a WARC before publishing its index.

WARC shards target 1 GB compressed and use sequences `001` through `999`.
A shard is written to an exclusive temporary path beneath `captures/.work/`,
closed, validated, and atomically moved into its portable collection.

Capture identity preserves URL key, original URL, raw timestamp, raw CDX status,
and raw CDX digest. Payload reuse is collection-local and same-URL only, keyed by
`(urlkey, valid CDX digest)`. Revisits may cross WARC shards inside a collection
but never cross collection boundaries or point forward in time.

Collection index validation requires every locator to be a basename naming one
of that collection's immutable WARCs, every byte range to be in bounds, and
every revisit to resolve to a same-collection full response at an equal or
earlier timestamp.

Legacy `archive/`, `sources/`, root `index.cdxj`, `collection.json`, or
`failures.json` layouts are rejected with a regenerate-required error. There is
no migration or dual-layout compatibility layer.

## Network policy

Playback uses one persistent client and one request at a time. Transport
failures, HTTP 5xx, and HTTP 429 receive at most three total attempts with retry
delays of 5 and 10 seconds unless `Retry-After` specifies a positive delay.
Exact mismatches, blocked captures, unusable bodies, truncated stored payloads,
and other permanent failures continue immediately.

Redirects and captures without valid CDX digests download individually. Fetch
requests exact timestamps and URLs, uses original/raw playback, never follows
historical redirects, and never substitutes a nearest capture.

## Fidelity: pass-through vs derived

Fetch reconstructs portable WARC/CDXJ collections from IA CDX plus exact
``id_`` playback. It does not download IA's internal WARCs, so output will not
match those files byte-for-byte. The contract is: for each well-formed CDX row
that exact playback can satisfy, store that capture's URL, time, status, and
payload in a pywb-playable form—without collapsing URL aliases (scheme/www) or
rewriting paths.

| Field / concern | Treatment |
|-----------------|-----------|
| CDX row selection | **Pass-through** — every well-formed row is scheduled; no http/https or www alias filtering |
| Original URL (`WARC-Target-URI`) | **Pass-through** — CDX original URL; only default ports (`:80` / `:443`) are stripped |
| Capture timestamp | **Pass-through** — CDX timestamp → `WARC-Date` |
| HTTP status | **Pass-through** — exact playback must match CDX status; retained as `CDX-Status` |
| CDX urlkey / digest | **Pass-through** — stored on the WARC as `CDX-Urlkey` / `CDX-Payload-Digest` |
| Payload body | **Pass-through** of exact `id_` entity bytes (after false-gzip repair when IA mis-labels encoding) |
| HTTP entity headers | **Modified** — drop representation headers (`Content-Encoding`, `Transfer-Encoding`, `ETag`, payload digests, etc.) and rewrite `Content-Length` so headers describe the stored body; keep `Content-Range` for HTTP 206 |
| Status reason / protocol line | **Derived** — synthesized (`200 OK`, `HTTP/1.1`); not taken from the archived reason phrase |
| Failed / unplayable CDX rows | **Omitted** — recorded in `run.json`; nothing written to WARC/CDXJ |
| Digest match | **Exact or soft** — exact body SHA-1, or early-IA quirk where CDX hashed `body + "\n"` while `id_` returns `body`; soft matches still store exact playback bytes and can seed revisits |
| Digest mismatch | **Kept** — body stored; `WARC-Payload-Digest` is of actual bytes; IA digest preserved; `CDX-Digest-Match: false`; cannot seed revisits |
| Same-urlkey revisits | **Derived** — collection-local identical-payload revisits; not IA's revisit graph |
| Year collections | **Derived** — calendar-year partition; revisits do not cross years |
| Collection CDXJ | **Derived** — indexed from finalized WARCs (`url`, `status`, `mime`, digests, offsets); not a copy of IA CDX |
| Record types / order | **Derived** — `warcinfo` + `response`/`revisit` only; shard order is write order, not crawl order |

## Deferred capabilities

Arbitrary grouping strategies, a public collection-ID CLI, remote publication,
catalogs, cross-collection deduplication, and overlapping-collection precedence
are intentionally deferred. The layout and publication APIs use generic IDs so
those features do not require another storage-format redesign.
