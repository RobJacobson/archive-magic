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

Defaults are the practical Wayback start (`19960101000000`) through the current
UTC time. Date bounds are UTC. Rate, concurrency, retries, and WARC size are
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
| `scheduler.py` | Ready/delayed queues, pacing, concurrency, 429 gate |
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

### Same-year reuse

Within a year, non-redirect captures with a usable CDX digest share one
successful representative response; later matches become revisits that resolve
inside the same annual WARC set. Redirects and digest-less rows are fetched
individually. Matching digests in different years each store a full response.

### Scheduler

- Ready queue ordered by `(timestamp, URL, identity)`
- Delayed retry queue by monotonic eligibility time
- Smooth playback start interval (`0.125s`, 8/s)
- Separate bounded in-flight concurrency (`MAX_IN_FLIGHT = 24`)
- Collection-wide 429 cooldown (`Retry-After` or exponential default)
- Workers release in-flight slots before waiting on retries
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
identity reuse, scheduler pacing/429, inventory skip, same-year revisits,
cross-year full responses, WARC rollover, crash index recovery, CDXJ merges,
and partial-run truthfulness. Navigator owns the real-pywb cross-shard revisit
integration test.

## Out of scope

Loose website files, local-link rewriting, redirect reports, coverage-window
merging, per-resource WARC paths, persistent shard indexes, cross-URL digest
deduplication, R2, and generalized job queues are not part of Fetch.
