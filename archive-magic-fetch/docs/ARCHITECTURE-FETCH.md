# Archive Magic Fetch architecture

Archive Magic Fetch builds immutable, portable WARC 1.1 collections and sorted
CDXJ indexes from Internet Archive history for one domain pattern. The current
CLI partitions captures by UTC calendar year, but collection publication is
identified by a generic filesystem-safe collection ID.

## Module ownership

Policy constants and data models are isolated in `policy.py` and `models.py`;
capture normalization lives in `identity.py`. `cdx.py` owns CDX acquisition,
while `playback.py` owns Wayback sessions and playback interpretation.
`inventory.py` derives local and cross-collection reuse state, `workers.py`
owns pacing and retries, and `resolution.py` applies chronological capture
selection without mutating collection state. `warc.py` owns only WARC record
construction, validation, salvage, and shard publication. `fetch.py` coordinates
startup recovery and serial per-year publication on the main writer thread.

## Command and output

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

The default range is 1995-01-01 through the current UTC time. `--start` and
`--end` accept the same calendar grammar as TOML: year, month, or day precision,
with or without hyphens (`1995`, `1995-01`, `1995-01-01`, or compact CDX digits).
A partial start expands to the beginning of that precision; a partial end expands
to its last instant (`--end 2004` is `20041231235959`). Omitted `--end` means
now. Expected capture-level failures do not fail the command; they are reported
in the corresponding immutable run record. Unexpected exceptions return nonzero.

```text
<archives-root>/example.org/
├── collections/
│   └── 2004/
│       ├── example.org-2004-001.warc.gz
│       ├── example.org-2004-001.warc.gz.partial
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

The requested range is normalized once to 14-digit UTC CDX timestamps, then
split into yearly slices. The first and last years are clipped to the requested
bounds; intervening years use the full calendar year. Years are processed
serially in ascending order. For each selected year Fetch:

1. Creates `captures/<year>/runs/<run-id>/` and durably saves every raw CDX HTTP
   entity as `page-NNN.cdx.gz` before parsing it.
2. Parses, validates, de-duplicates exact CDX rows, orders them by timestamp,
   and groups them by URL key.
3. Inventories only finalized WARCs in `collections/<year>/`.
4. Resolves URL groups through bounded playback workers. Fully represented
   groups stay on the main thread so they do not occupy workers. Each group
   remains chronological: represented identities are skipped, an earlier matched
   response with the same URL key, valid CDX digest, and CDX status becomes a
   revisit, HTTP 200 captures whose CDX digest is the empty payload are
   materialized locally, and an eligible earlier-collection HTTP 200 payload is
   copied into a new full response. A CDX 301/302 whose URL group already lists
   this URL plus a trailing slash is stored as that slash redirect without
   playback. Other captures use exact playback. When exact `id_` playback of a
   CDX 301/302 is a Wayback `found capture at …` 302 whose Location is this URL
   plus a trailing slash, Fetch stores a redirect with that `Location` instead
   of following the nearby capture. Other `found capture at …` substitutions
   are fetched once and stored under the CDX identity as inexact keeps.
5. Finalizes the current shard in `collections/<year>/` (continuing the last
   shard when it is under the 1 GB cap), rebuilds the collection CDXJ from every
   finalized WARC, and atomically publishes it.
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
missing captures. In-progress shards are written as visible
`*.warc.gz.partial` siblings in `collections/<year>/`. Startup salvage truncates
a torn last gzip member and promotes a usable partial to its finalized name.
Ctrl-C and unexpected failures finalize the open shard and rebuild CDXJ, but
they do not write `run.json`. A collection directory without a replay index is
not playable; Navigator skips it.

The last shard is continued when it is under 1 GB compressed. A new sequence
starts only when that cap is reached. Collection CDXJ is always rebuilt from
the finalized WARCs; offsets are not preserved across publication.

WARC shards target 1 GB compressed and use sequences `001` through `999`.
A shard is written to `collections/<year>/<archive>-<year>-NNN.warc.gz.partial`,
closed, validated, and atomically renamed into its portable collection name.

Capture identity preserves URL key, original URL, raw timestamp, raw CDX status,
and raw CDX digest. Revisit reuse is collection-local and same-URL only, keyed by
`(urlkey, valid CDX digest, CDX status)`. Payload reuse across collections is
keyed only by the valid IA digest, is limited to HTTP 200, and writes a new full
response into the current collection. Revisits may cross WARC shards inside a
collection but never cross collection boundaries or point forward in time.

Collection index validation requires every locator to be a basename naming one
of that collection's immutable WARCs, every byte range to be in bounds, and
every revisit to resolve to a same-collection full response at an equal or
earlier timestamp.

Legacy `archive/`, `sources/`, root `index.cdxj`, `collection.json`, or
`failures.json` layouts are rejected with a regenerate-required error. There is
no migration or dual-layout compatibility layer.

## Network policy

Playback uses `PLAYBACK_WORKERS` persistent worker clients. One shared gate
smoothly limits starts to `PLAYBACK_STARTS_PER_SECOND`; retries pass through the
same gate. Transport failures and HTTP 5xx receive at most
`MAX_PLAYBACK_ATTEMPTS`, using exponential retry delays.

HTTP 429 and refused TCP connections are both treated as IA backpressure. They
pause all new starts: 429 honors `Retry-After` or defaults to
`BACKPRESSURE_COOLDOWN_S`, while TCP refusal uses that default. Concurrent
signals retain the largest observed delay and extend the shared monotonic
deadline. Exact mismatches, blocked captures, unusable bodies, truncated stored
payloads, and other permanent failures continue immediately.

Workers return completed URL groups without printing or mutating collection
state. URL groups that need no Wayback GET (fully represented, empty HTTP 200
payloads, or slash-normalizing redirects already listed in the same CDX group)
are resolved on the main thread and never queued on playback workers. After a
download group finishes, those local-only groups are yielded and printed before
the next Wayback GET is submitted, so skip tables are not held behind the
following playback. The main thread writes each group to WARC and then prints
one contiguous URL table with ISO-formatted capture timestamps linked to exact
``id_`` Wayback playback and the final six characters of each CDX payload
digest.

Captures without a valid CDX digest download individually. An earlier
digest-matched response with the same URL key, digest, and CDX status becomes a
revisit, including empty-bodied 301/302 captures. A 301 does not stand in for a
302 or a statusless row. Revisit records omit HTTP headers; pywb fills them from
the referred response, so later redirects inherit that capture's `Location`.
HTTP 200 captures whose CDX digest is the SHA-1 of zero bytes are materialized
locally (`Content-Type` from CDX MIME, `Content-Length: 0`) without playback.
Redirects with that digest still download once so `Location` is captured, unless
the same URL group already lists this URL plus a trailing slash. In that case
Fetch writes the slash redirect locally (`Location`, empty body) and does not
ask Wayback. If exact playback of a CDX 301/302 cannot be played and Wayback
instead returns a live 302 with `X-Archive-Redirect-Reason: found capture at …`
pointing at this URL plus a trailing slash, Fetch reconstructs that redirect
locally and does not store the nearby capture. Other `found capture at …` 302s
are followed once: Fetch stores the nearby memento under the CDX identity
(WARC-Date and WARC-Target-URI stay the requested capture; WARC-Source-URI is
the memento actually fetched). URL, timestamp, status, or digest may disagree;
digest mismatches do not seed revisits. Fetch uses original/raw playback and
never follows historical redirects of an archived page.
Cross-year payload reuse stays HTTP 200 only. Unusable playback means IA
`Invalid URI` stubs and empty bodies that contradict a non-empty CDX digest.

## Digest domains and payload reuse

Fetch deliberately uses two digest domains. The IA/CDX payload digest is source
metadata used at acquisition edges: it remains part of capture identity and is
the key for comparing captures against the cross-year payload cache. The WARC
payload digest is recomputed from the exact bytes Fetch stores and is used for
internal integrity, revisit references, and the standard CDXJ `digest` field.
Generated CDXJ response entries retain both domains as `cdxDigest` and `digest`,
plus `cdxDigestMatch` to identify responses eligible to seed payload reuse.

Some early IA ARC indexes hashed `payload + "\n"` while playback returns
`payload`. Fetch accepts this recognized discrepancy as a soft match, stores
the playback bytes, and retains IA's original digest as the cache key. It does
not create aliases between IA digests: identical bytes advertised under the
correct and newline-adjusted hashes miss each other and may be downloaded more
than once. This is a safe false negative. Other explicit digest mismatches are
kept for capture fidelity but never seed revisits or payload reuse.

Same-URL revisits are a separate path from the payload cache. They may include
redirects and other non-200 statuses because they stay on one URL and one CDX
status. They inherit HTTP headers (including `Location`) from the representative
via pywb. HTTP 200 captures whose CDX digest is the SHA-1 of zero bytes skip
playback entirely: Fetch writes a full response with an empty body and
synthesized `Content-Type` / `Content-Length`, then later same-key captures
become revisits. A 301/302 whose URL group already contains this URL plus a
trailing slash is the same kind of local materialization: Fetch writes
`Location` and an empty body without asking Wayback.

The payload cache is an in-memory, rebuildable acquisition accelerator derived
from finalized CDXJ locators. Its keys intentionally omit URL. Reuse is limited
to HTTP 200 captures because a payload digest says nothing about status-specific
metadata such as `Location` or `Content-Range`. An empty-body digest therefore
cannot attach another capture's redirect metadata to a different URL. A hit
range-reads and validates the earlier full response, verifies that its WARC
timestamp matches the CDXJ timestamp and is not later than the current capture,
then writes a new full response with the current capture identity, status, and
CDX MIME. Only `Content-Type` and a recomputed `Content-Length` are synthesized;
headers from the representative capture are not copied across captures. This
makes each yearly collection independently replayable without attributing another
capture's cookies, security policy, or other HTTP metadata to the current one.
Missing, corrupt, future, or invalid candidates are ordinary cache misses and
fall back to exact playback. CDXJs without the explicit `cdxDigest` and
`cdxDigestMatch` fields do not seed the cache; there is no compatibility or
migration path.

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
| Original URL (`WARC-Target-URI`) | **Pass-through** — CDX original URL; only default ports (`:80` / `:443`) are stripped. Exact-playback URL checks treat percent-encoding variants as equal (IA may double-encode in `Link: rel="original"`) |
| Capture timestamp | **Pass-through** — CDX timestamp → `WARC-Date` |
| HTTP status | **Pass-through** — exact playback must match CDX status; retained as `CDX-Status` |
| CDX urlkey / digest | **Pass-through** — stored on the WARC as `CDX-Urlkey` / `CDX-Payload-Digest` |
| Payload body | **Pass-through** of exact `id_` entity bytes (after false-gzip repair when IA mis-labels encoding). HTTP 200 with the empty CDX digest is synthesized as zero bytes without playback. CDX 301/302 rows that Wayback will not play exactly, substituting a nearby trailing-slash capture, are stored as an empty redirect with reconstructed `Location` |
| HTTP entity headers | **Modified** — drop representation headers (`Content-Encoding`, `Transfer-Encoding`, `ETag`, payload digests, etc.) and rewrite `Content-Length` so headers describe the stored body; keep `Content-Range` for HTTP 206 |
| Status reason / protocol line | **Derived** — synthesized (`200 OK`, `HTTP/1.1`); not taken from the archived reason phrase |
| Failed / unplayable CDX rows | **Omitted** — recorded in `run.json`; nothing written to WARC/CDXJ. Includes IA `Invalid URI` stubs and empty playback that contradicts a non-empty CDX digest |
| Digest match | **Exact or soft** — exact body SHA-1, or early-IA quirk where CDX hashed `body + "\n"` while `id_` returns `body`; soft matches still store exact playback bytes and can seed revisits and payload reuse. HTTP 200 with the empty CDX digest is materialized locally without playback |
| Digest mismatch | **Kept** — body stored; `WARC-Payload-Digest` is of actual bytes; IA digest preserved; `CDX-Digest-Match: false`; cannot seed revisits or payload reuse |
| Same-urlkey revisits | **Derived** — collection-local identical-payload revisits keyed by URL key, CDX digest, and CDX status (including redirects); not IA's revisit graph. Revisit records omit HTTP headers, so pywb inherits `Location` from the representative |
| Cross-year payload reuse | **Derived** — digest-only IA lookup for HTTP 200 captures; the cached body, current CDX MIME, and recomputed length become a new full response in the current year |
| Year collections | **Derived** — calendar-year partition; revisits do not cross years and every year retains a full copy of reused payload data |
| Collection CDXJ | **Derived** — indexed from finalized WARCs (`url`, `status`, `mime`, local/IA digests, offsets); not a copy of IA CDX |
| Record types / order | **Derived** — `warcinfo` + `response`/`revisit` only; shard order is write order, not crawl order |

## Deferred capabilities

Arbitrary grouping strategies, a public collection-ID CLI, remote publication,
persistent catalogs, cross-collection revisits, and overlapping-collection
precedence are intentionally deferred. The layout and publication APIs use
generic IDs so those features do not require another storage-format redesign.
