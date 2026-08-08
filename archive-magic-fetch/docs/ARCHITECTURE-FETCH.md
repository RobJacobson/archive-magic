# Archive Magic Fetch architecture

Archive Magic Fetch builds annual, size-bounded WARC 1.1 collections and CDXJ
indexes from Internet Archive history for one website pattern.

Authoritative product decisions live in [HANDOFF-FETCH-AUDIT.md](HANDOFF-FETCH-AUDIT.md).
This document describes the finished implementation.

## Job

1. Query Internet Archive CDX metadata (via `wayback` policy, with raw entity
   bytes preserved before any local normalization).
2. Decide which exact captures need playback.
3. Fetch those captures through a polite, bounded scheduler.
4. Write selected history into annual, size-bounded WARC 1.1 files.
5. Publish annual and collection-wide CDXJ indexes for pywb/Navigator playback.

## Command

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

Defaults are the practical Wayback start (`19950101000000`) through the current
UTC time. Date bounds are UTC. Rate, connections, retries, and WARC size are
named constants in `models.py`, not CLI options.

Exit status:

- `0`: every selected capture is represented and publications succeeded
- nonzero: fatal error or unresolved captures remain

Finalized WARCs and indexes from a partial run remain usable.

## Collection layout

```text
<archives-root>/
└── example.org/
    ├── collection.json
    ├── failures.json                 # only when unresolved failures exist
    ├── archive/
    │   └── 2004/
    │       ├── example.org-2004-001.warc.gz
    │       └── example.org-2004-002.warc.gz
    ├── indexes/
    │   ├── years/
    │   │   └── 2004.cdxj
    │   └── index.cdxj
    └── sources/
        └── <UTC-run-id>/
            ├── query.json
            └── 2004.cdx
```

CDXJ `filename` values are collection-relative POSIX paths. Temporary work lives
under `.work/` or `.tmp-*` names cleaned on startup.

## Modules

| Module | Role |
|--------|------|
| `cli.py` | Argument parsing and exit status |
| `models.py` | Capture identity, results, policy constants |
| `cdx.py` | Annual CDX query, raw persistence, parse |
| `scheduler.py` | Ready/delayed queues, pacing, connection budget, global backpressure gates |
| `warc.py` | Inventory, exact playback, WARC 1.1 write/rollover |
| `index.py` | Per-WARC fragments, annual merge, collection merge |
| `collection.py` | Paths, atomic publish, manifest, failures |
| `fetch.py` | Year-ascending orchestration |

## Pipeline

```text
CLI
 → validate dates / derive collection id
 → cleanup temps; reconcile missing annual indexes
 → for each year ascending:
      CDX raw preserve + parse
      inventory reuse (exact identity)
      same-year representative / revisit plan
      scheduler downloads + single WARC writer
      finalize WARC → annual CDXJ → partial manifest
 → collection CDXJ + final manifest/failures
```

### Capture identity

Identity is one type shared by CDX, inventory, failures, and validation:

- canonical URL key and original URL
- raw 14-digit timestamp
- raw CDX status token (`-` for statusless)
- raw CDX payload digest (or missing sentinel)

WARC extension headers:

- `CDX-Status` — raw CDX status token (not the numeric HTTP status alone)
- `CDX-Payload-Digest` — IA CDX digest (distinct from `WARC-Payload-Digest`)

### Exact playback

Requests use original/raw mode, `exact=True`, and `follow_redirects=False`.
URL/timestamp mismatches and known-status mismatches are rejections, not
silent substitutions.

### Network ownership

Archive Magic, rather than `wayback`, owns playback pacing and retries:

- `ArchiveMagicWaybackSession` sets the library retry count to zero. Nested
  library and scheduler retry loops would make request volume and delays hard
  to reason about.
- The scheduler is the single process-wide authority for request starts, the
  connection budget, delayed retries, HTTP 429 cooldowns, and TCP-refusal
  cooldowns.
- `MAX_CONNECTIONS` is the playback TCP budget: the number of concurrent
  connections Archive Magic may open to `web.archive.org` for memento
  download. It is implemented as N worker threads, each holding one
  thread-local `WaybackClient` whose urllib3 pool is capped at size 1. There is
  no single shared urllib3 pool across threads because `WaybackSession` is not
  thread-safe.
- Each active worker lazily creates its client on first use and keeps it for
  the run. DNS/TCP/TLS begins on that worker's first request, not when the
  executor is constructed.
- Persistent per-worker sessions allow urllib3 to reuse HTTP connections.
  There is never one session per capture. A session may still need a
  replacement connection when IA closes a socket or a transfer fails.
- All requests identify Archive Magic with the descriptive `USER_AGENT` from
  `models.py`.

CDX acquisition is separate from playback scheduling. Annual CDX pages are
queried serially with one session. That code owns an eight-attempt loop:
`Retry-After` (or 60 seconds) for 429, and exponential delays from 5 seconds up
to 300 seconds for connection failures and 5xx responses. Raw HTTP entity bytes
are durably published before parsing.

### Playback rate and connection pool

`PLAYBACK_REQUESTS_PER_SECOND` is the policy input; the scheduler derives the
minimum interval as its reciprocal. Expressing policy as requests per second
keeps configuration and throughput calculations obvious.

`MAX_CONNECTIONS` is the connection budget: how many TCP connections playback
may use concurrently. Each connection is owned by one worker session.

Current defaults:

- `PLAYBACK_REQUESTS_PER_SECOND = 4.0`
- `MAX_CONNECTIONS = 8`
- `MAX_PLAYBACK_ATTEMPTS = 9` (one first attempt and up to eight retries)

The start rate and connection budget are independent:

- The rate controls when work may begin, including retries.
- The connection budget controls how many transfers may hold a socket at once.
- If every connection slot is occupied, achieved throughput falls below the
  configured start rate; the scheduler never creates extra workers to catch up.
- After any delay, the next interval is measured from the actual request start,
  so accumulated delay cannot turn into a burst.

There is no token bucket or adaptive boundary-seeking controller. One central
scheduler already owns every start, so smooth interval pacing is sufficient.
The fixed baseline is intentionally paired with explicit backpressure handling
rather than repeatedly probing IA's current limit.

### Why these defaults

Internet Archive's public guidance asks automated clients to add delays, limit
concurrency, honor HTTP 429 and `Retry-After`, and use exponential backoff. It
does not publish a stable numeric Wayback playback quota. The old `wayback`
library assumption of 600 mementos/minute is therefore not treated as a
contract.

Local measurements established these operational facts:

- Persistent connections completed 2,123 small playbacks through a full minute
  at 8 requests/second without a 429 or TCP refusal.
- Creating a fresh session for every request produced a TCP refusal on the
  eighteenth connection even at only 1 request/second.
- A persistent test sustained 8 requests/second with nine active sessions. A
  12 requests/second target increased latency, activated all 24 workers used by
  that test, and caused new connections to be refused while achieved
  throughput remained near 8 requests/second.
- Real heterogeneous workloads eventually replace connections even with
  persistent sessions. A session or connection cap alone therefore cannot
  prevent a rolling connection-admission limit from being reached.
- A single-connection budget (`MAX_CONNECTIONS = 1`) increases idle time
  between requests, which lets keep-alive sockets die and forces more TCP
  handshakes—raising refusal rates even at a low start rate.

These observations point to connection creation/churn as an important limit,
not a simple requests-per-second boundary. They are empirical safeguards, not
a claim about IA's internal implementation. The production defaults are
4 starts/second and 8 concurrent connections, while the global refusal gate
provides recovery when conditions change.

### Backpressure gates

HTTP 429 and TCP connection refusal are distinct signals that share the global
`blocked_until` gate. The gate applies equally to first attempts and retries.

For HTTP 429:

- `ArchiveMagicWaybackSession.send()` explicitly checks status 429. This is
  necessary because `wayback` otherwise treats any response carrying
  `Memento-Datetime` as a successful memento.
- A `RateLimitError` carries the response and recommended delay into the
  scheduler.
- The scheduler prefers the response's `Retry-After`, then a delay carried by
  the exception, then the 60-second default.
- The global 429 cooldown is bounded by `MAX_429_COOLDOWN_S` (900 seconds).
- Error-chain inspection avoids treating the digits `429` inside a capture
  timestamp such as `20080429...` as a rate-limit response.

For refused TCP connections:

- A refusal occurs before HTTP, so it cannot supply status 429 or
  `Retry-After`. Archive Magic nevertheless treats `ConnectionRefusedError`,
  including one wrapped by requests/urllib3/wayback, as IA backpressure.
- The first refusal pauses all request starts for 5 seconds. A refusal from a
  request started after that cooldown escalates subsequent waves to 10, 20,
  40, and finally 60 seconds.
- Refusals from requests already using a connection slot belong to the wave in
  which those requests started. Late completions therefore do not multiply the
  cooldown.
- The refusal-wave delay remains capped at 60 seconds for the rest of that
  scheduler run.

The global cooldown prevents a positive-feedback loop: without it, every
refusal would enqueue a retry while untouched captures continued starting,
creating more refusals and more delayed work.

### Retry and permanent-failure policy

Retryable playback failures use scheduler-owned exponential delay:
10, 20, 40, ... seconds, capped at `MAX_RETRY_DELAY_S` (3600 seconds). A supplied
retry delay takes precedence. Untouched first attempts remain ahead of promoted
retries so retries cannot monopolize a large run. Every promoted retry still
passes through start pacing, the connection budget, and global cooldowns.

Not every broken transfer is transient:

- IA may contain permanently truncated captures, commonly PDFs, where the
  advertised length exceeds the stored bytes and playback repeatedly raises
  `IncompleteRead`.
- requests may wrap that as `ChunkedEncodingError`, and `wayback` may wrap it
  again. Classification inspects the complete outer error before generic
  connection handling.
- Such captures are recorded immediately as non-retryable `TRUNCATED`
  failures. Re-downloading them would consume bandwidth and replacement
  connections without producing a complete payload.

Blocked captures, exact-playback mismatches, digest mismatches, and ordinary
permanent playback failures are also not retried. Retryable 5xx, timeout,
connection, and rate-limit failures remain eligible up to the attempt limit.
Permanent or exhausted failures flow through the bounded result handoff into
`failures.json`; they are never silently discarded.

### Logging and metrics

Retry, 429, and connection-refusal messages include the full wrapped exception
so the terminal preserves the decisive inner cause. Logs distinguish HTTP rate
limits from pre-HTTP TCP refusal. `collection.json` records playback starts and
completions, bytes, peak concurrent connections, rate-gate wait, cooldown wait,
and attempt counts by stable failure category.

### Same-year reuse

Within a year, non-redirect captures with a usable CDX digest share one
successful representative response; later matches become revisits that resolve
inside the same annual WARC set. Redirects and digest-less rows are fetched
individually. Matching digests in different years each store a full response.

### Scheduler

- Ready queue ordered by `(timestamp, URL, identity)`
- Delayed retry queue by monotonic eligibility time
- Smooth request-start pacing derived from requests/second
- Separate `MAX_CONNECTIONS` budget (N worker threads, pool size 1 each)
- Collection-wide HTTP 429 and TCP-refusal cooldowns
- Workers release connection slots before waiting on retries
- First attempts precede promoted retries; both pass through the same gates
- Thread-local persistent Wayback clients are closed after the scheduler drains
- One WARC writer consumes validated results through a bounded handoff

### Publication order

1. immutable finalized WARC
2. atomic annual CDXJ update
3. `collection.json` describing current partial/complete state
4. collection CDXJ after the year completes
5. final manifest and `failures.json` when unresolved entries remain

## Navigator boundary

Navigator expects `indexes/index.cdxj` and resolves CDXJ `filename` values under
the collection root. It does not reindex or rewrite Fetch output.

## Testing

High-value tests in `tests/test_core.py` cover raw CDX preservation, statusless
identity reuse, scheduler pacing, HTTP 429 handling, escalating TCP-refusal
waves, wrapped permanent truncation, inventory skip, same-year revisits,
cross-year full responses, WARC rollover, crash index recovery, CDXJ merges,
and partial-run truthfulness. Navigator owns the real-pywb cross-shard revisit
integration test.

## Out of scope

Loose website files, local-link rewriting, redirect reports, coverage-window
merging, per-resource WARC paths, persistent shard indexes, cross-URL digest
deduplication, R2, and generalized job queues are not part of Fetch.
