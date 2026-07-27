---
name: Fetch concurrency design
overview: Add bounded threaded memento retrieval (default 8 workers) that overlaps download latency under the official 8 req/s Wayback pacing, while keeping WARC/file writes and within-group deduplication strictly ordered on the main thread.
todos:
  - id: retrieval-pool
    content: Add thread-safe RetrievalCache, shared RateLimitGate, and retrieve_mementos() with ThreadPoolExecutor + per-worker WaybackClient sharing RateLimit(8)
    status: completed
  - id: export-pipeline
    content: Refactor _export_group to plan unique fetches, parallel retrieve, then ordered write/dedup
    status: completed
  - id: files-pipeline
    content: Use the same prefetch helper for write_website_files (plan order writes)
    status: completed
  - id: cli-concurrency
    content: Add --concurrency default 8 (1 = serial diagnostic mode) and plumb through CLI
    status: completed
  - id: tests-docs
    content: Cover ordering, no duplicate fetch for same CDX signature, coordinated 429 pause; update ARCHITECTURE-FETCH.md
    status: completed
isProject: false
---

# Concurrent Fetch Design

## What the official client actually does

You are using EDGI’s [`wayback`](https://wayback.readthedocs.io/en/stable/) package (`wayback==0.5.1`), not `internetarchive`. Its pacing is built into `WaybackSession.send()`:

| Endpoint | Default | Meaning |
| --- | --- | --- |
| CDX `/cdx` | `0.4` calls/s | ~1 every 2.5s |
| Memento `/web/...` | `8` calls/s | **1 every 125ms (1/8s)** |

Those defaults come from IA staff guidance: hard caps ~60/min CDX and ~600/min mementos; client aims for ~80% of that → **48/min CDX, 480/min mementos (= 8/s)**.

Important mechanics from local source (`wayback._utils.RateLimit`):

- `wait()` uses a thread-safe `RLock`.
- It paces **request starts**, then returns; download time is not part of the wait.
- So serial Fetch is slow because each round-trip+body download blocks the next start, even though the limiter would allow another start every 125ms.
- Concurrency of about **8 in-flight mementos** is the natural match: workers block on `RateLimit.wait()`, then overlap body download.

`RateLimitError` (HTTP 429) is raised to the caller; with threads, **one worker’s 429 does not automatically pause the others**. Our current one-retry policy in [`retrieval.py`](archive-magic-fetch/src/archive_magic_fetch/retrieval.py) must become a shared “pause the pool” gate.

Discovery can stay serial: one `search()` stream already materializes under the CDX limiter; concurrency wins almost nothing there.

## Do we care about ordering?

**Write order: yes. Fetch completion order: no.**

Current contract in [`ARCHITECTURE-FETCH.md`](archive-magic-fetch/docs/ARCHITECTURE-FETCH.md) §9:

1. URL-key groups in sorted order within a WARC bucket
2. Captures in timestamp order within a group
3. Dedup maps updated only after successful validation/write

That ordering is useful for WARC debugging and keeps revisits deterministic. Replay CDXJ is sorted later anyway, so record order is not a replay correctness requirement—but preserving it is cheap if writes stay serial.

Your “Promise.all then write in order” mental model is right:

```mermaid
flowchart LR
  plan[Plan needed fetches] --> pool[ThreadPool workers]
  pool --> cache[Result map or RetrievalCache]
  cache --> write[Main thread ordered write and dedup]
```

## Python concurrency models (short education)

| Approach | Fit for us |
| --- | --- |
| `asyncio` + `aiohttp`/`httpx` | Poor fit. `wayback` is sync `requests`. Would mean rewriting the client layer. |
| `multiprocessing` | Overkill; no CPU win, harder shared rate limits, higher memory. |
| `concurrent.futures.ThreadPoolExecutor` | **Best fit.** I/O-bound downloads release the GIL; maps cleanly to JS `Promise.all`. |
| One thread per request unbounded | Bad: memory + 429 risk. Bound workers. |

JS analogy:

```text
Promise.all(urls.map(fetch))     ≈  executor.map(retrieve_memento, captures)
await then write in order        ≈  main thread consumes results by index/key
```

## Recommended approach (concrete)

**Pipeline: parallel retrieve, serial decide/write.**

Keep all WARC/file policy on the main thread. Only network retrieval becomes concurrent.

### 1. Bound workers to the memento limiter

- Default `--concurrency 8` (or env/default constant `8`).
- `--concurrency 1` restores today’s serial diagnostic behavior.
- Workers > 8 mostly queue inside `RateLimit.wait()`; little throughput gain, more peak memory.

### 2. Prefer one client per worker, shared `RateLimit` instances

`requests.Session` is not fully thread-safe if shared for concurrent in-flight calls. `wayback` already documents sharing limits across sessions:

```python
from wayback import RateLimit, WaybackSession, WaybackClient

memento_limit = RateLimit(8)   # or keep library defaults via shared defaults
search_limit = RateLimit(0.4)

def make_client():
    return WaybackClient(WaybackSession(
        user_agent=USER_AGENT,
        search_calls_per_second=search_limit,
        memento_calls_per_second=memento_limit,
    ))
```

Discovery keeps one client. Export/files workers each get a thread-local client sharing those limiters.

### 3. Plan unique fetches before firing the pool

Within a URL-key group, serial export currently skips later same CDX digest/status as revisits **without** fetching. Naive “fetch all eligible captures in parallel” would waste bandwidth.

For each group (or for the dual-mode union of warc+files targets):

1. Walk captures in timestamp order.
2. Mark captures that are CDX-known revisits of an earlier signature as **no fetch**.
3. Submit only first-seen source signatures (and statusless digests that need a first body) to the pool.
4. After results return, walk again in order: write response/revisit, apply semantic dedup, update maps—same logic as today in [`export.py`](archive-magic-fetch/src/archive_magic_fetch/export.py) `_export_group`.

Files mode ([`files.py`](archive-magic-fetch/src/archive_magic_fetch/files.py)) is simpler: plan targets are already unique paths; prefetch those captures (via shared [`RetrievalCache`](archive-magic-fetch/src/archive_magic_fetch/retrieval.py)), then write in plan order.

### 4. Batching strategy

Start with **per URL-key group** (WARC) or **per website plan batch** (files):

- Plan needed captures for the next group/batch.
- `executor.map` / submit+wait.
- Ordered write.
- Next group.

Optional later optimization: prefetch group N+1 while writing group N. Not needed for v1.

Do **not** parallelize WARC writers for the same file. One gzip stream, one `warcio` writer.

### 5. Shared rate-limit / error gate

Replace per-call `time.sleep` in `_get_memento_with_retry` with a small process-wide gate:

- On first `RateLimitError`: lock, sleep `retry_after or 60`, mark “one retry allowed”, release.
- In-flight workers that also hit 429 wait on the same lock (single pause).
- A second 429 after the coordinated pause remains fatal for the job (current policy).

Skip-able playback errors (`MementoPlaybackError`, robots, etc.) stay per-capture; only rate limits are global.

### 6. Make `RetrievalCache` thread-safe

Today’s cache is single-threaded. Under a pool:

- Lock around get/set.
- Optional: single-flight per capture key so two workers don’t fetch the same capture twice when warc+files share work.

### 7. Memory and diagnostics

- Cap in-flight bodies with `max_workers` (default 8). Bodies are held until the batch write finishes—acceptable for v1.
- Progress lines can still print in write order (`Downloaded …`), so logs stay chronological even if fetches finish out of order.
- Keep concurrency out of discovery and out of WARC open/close/indexing.

## Issues beyond rate limits

- **Dedup correctness:** parallel fetch must not bypass first-success → revisit planning.
- **Session safety:** don’t share one `WaybackSession` across concurrent in-flight GETs; share `RateLimit` instead.
- **429 amplification:** uncoordinated retries from N workers can extend IP blocks; shared pause is required.
- **Fatal vs skip:** unchanged—rate-limit exhaustion fatal; ordinary playback failures skip.
- **Tests:** fake clients + `ThreadPoolExecutor` with injectable sleep/gate; assert write order and “duplicate digest not fetched twice” within a group.
- **Docs:** remove “serial retrieval” / “concurrent retrieval” non-goal from [`ARCHITECTURE-FETCH.md`](archive-magic-fetch/docs/ARCHITECTURE-FETCH.md); document `--concurrency` and that write order remains sorted urlkey + timestamp.

## Suggested implementation shape (when you greenlight code)

1. Add `RateLimitGate` + thread-safe `RetrievalCache` in `retrieval.py`.
2. Add `retrieve_mementos(captures, client_factory, max_workers)` helper.
3. Refactor `_export_group` to plan → parallel retrieve → ordered write.
4. Refactor `write_website_files` similarly (or just warm cache via the helper).
5. Wire `--concurrency` in [`cli.py`](archive-magic-fetch/src/archive_magic_fetch/cli.py) (default 8).
6. Update architecture doc + tests.

## Expected speedup

Roughly: if average memento download is `D` seconds, serial effective rate ≈ `1 / (0.125 + D)` starts/s. With 8 workers under an 8/s start limiter, you approach **~8 starts/s** when `D` is large—often several× faster on real IA latency, without exceeding the official pacing.