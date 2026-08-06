# Archive Magic Fetch audit handoff

## Purpose

This memo hands the Fetch audit to a new reviewer who should reassess the
remaining issues against the current code rather than continue the design
momentum of the earlier review. The user's priorities are, in order:

1. preserve every capture that can reliably be obtained from Internet Archive;
2. avoid duplicate or unnecessary network retrieval;
3. obtain major performance improvements without sacrificing correctness;
4. keep the implementation DRY, YAGNI, and KISS.

The current branch is `refactor-fetch-code`. At the time of this memo, its tip
is `7807138` (`feat(fetch): enhance capture identity and playback handling`).
The Fetch suite has 272 passing tests and the Navigator suite has 82 passing
tests.

Start with [ARCHITECTURE-FETCH.md](ARCHITECTURE-FETCH.md), then inspect the
implementation. Treat this memo as a list of questions and evidence, not as a
preapproved implementation plan.

## Decisions already made

Do not casually reopen these decisions:

- This is a clean-slate project. There is no compatibility code for old
  manifests, old WARC headers, or removed CLI options.
- `--build-warc true|false` replaces `--warc`; it defaults to `true`.
- WARC output includes the complete selected IA history. There is no
  `latest` WARC mode.
- `--fresh` and `--redirect-capture` were removed without aliases.
- Playback is exact-only: `Mode.original`, `exact=True`, and
  `follow_redirects=False`. Do not introduce nearest playback or a separate
  "CDX Prime" mapping without a new design discussion.
- Existing local captures are retained even if IA no longer returns them.
- Append-only means a semantic superset of capture identities, not byte-for-
  byte preservation of WARC records.
- Redirects are stored but not followed. `redirects.json` lets the operator
  decide which external sites to capture in another pass.
- Whole-collection staging and folder-layout changes were explicitly deferred.
- Cross-URL digest deduplication, retry scheduling, worker/rate experiments,
  and WARC sharding were left outside the completed identity refactor.

## What the earlier audit resolved

| Earlier concern | Current disposition |
| --- | --- |
| Duplicate CDX rows or spelling-only URL differences could cause duplicate work | Resolved through deterministic CDX/WARC identities and normalized URL keys. |
| Existing captures absent from the current CDX result could be lost | Resolved: affected WARCs are rebuilt from the union of validated local records and current CDX rows. |
| Cache lookup used inconsistent identities | Resolved through `get_cdx_identity()` and `get_warc_identity()`. See the statusless exception below. |
| An unreadable existing WARC might be replaced by an IA-only reconstruction | Resolved: inventory failure is fatal and the old WARC remains untouched. |
| Partial rebuilding could drop old records | Resolved per WARC: the temporary is validated and must contain every prior logical identity before atomic replacement. |
| Replay-index offsets could refer to pre-rebuild WARCs | Resolved: the final replay index is regenerated from all final WARCs. |
| CDX and downloaded payload digests were conflated | Resolved: `CDX-Payload-Digest` represents IA's index value; `WARC-Payload-Digest` validates stored bytes and drives replay/revisit references. |
| An exact replay failure could be avoidable when a later capture proves the same payload | Mitigated: same-URL, same-CDX-digest failures can be recovered locally as revisits after another exact success. |
| Nearest playback could silently substitute a remote capture | Resolved by exact-only requests plus explicit returned URL, timestamp, and status validation. |
| Redirect expansion was too blunt and could explode scope | Resolved: automatic expansion was removed and a final-collection redirect report was added. |
| Failure headline and detail counts disagreed | Resolved by identity-based accounting. The old nclr.org log predates this fix. |
| `--fresh` semantics and staging-directory design were distracting the audit | `--fresh` was removed; staging remains a separately deferred reliability project. |
| Old revisits might be regenerated rather than byte-preserved | Accepted as inconsequential so long as semantic identities and payload references remain correct. |
| IA removals could leave obsolete local records | Accepted and desirable: local history is intentionally not deleted. |

## Highest-priority correctness checks

### 1. Statusless CDX rows appear non-idempotent

This should be reproduced before any more performance work.

`CaptureIdentity.status_code` is part of identity. `get_cdx_identity()` retains
`None` for a CDX row whose status is `-`, which is common for IA revisit rows.
Generated response/revisit records must contain a numeric HTTP status, so
`get_warc_identity()` reads an integer from the WARC. The same capture can
therefore have these two identities:

```text
CDX:  (..., status=None, digest=D)
WARC: (..., status=200,  digest=D)
```

The existing test `test_statusless_captures_with_matching_digest_use_revisit`
only checks the first build. Add a second and third identical build and verify:

- no playback request occurs;
- no duplicate revisit is appended during each semantic rebuild;
- response/revisit counts remain stable;
- every selected CDX identity is represented in the final WARC inventory.

Likely solutions include a required `CDX-Status-Code` header with a null
sentinel, or a carefully revised identity definition. Do not paper over this
by dropping status from identity: distinct same-time captures with different
known statuses must remain distinguishable. Prefer one small deterministic
rule shared by the two identity functions.

Also consider adding `selected identities ⊆ rebuilt identities` as a publication
postcondition. The current superset assertion only protects prior local
identities.

### 2. Saved "source CDX" is not actually raw IA CDX

The approved design says raw IA CDX remains authoritative and unchanged, but
the current search path materializes `wayback.CdxRecord` objects and later
serializes them again. The installed `wayback` library transforms input while
parsing:

- it heuristically repairs invalid month/day `00` timestamps;
- it removes redundant HTTP/HTTPS ports from original URLs;
- with the default `skip_malformed_results=True`, it omits URLs it considers
  malformed;
- it converts raw status/length tokens into typed values.

The nclr.org log contains:

```text
found invalid timestamp with day 00: 20000800310551
```

The saved file contains the library's approximation instead:

```text
org,nclr)/about/me.html 20000831055100 ...
```

This is not merely presentation normalization. It changes the timestamp used
for exact playback and means the durable source file cannot prove what IA
actually returned. Review whether Fetch should save the byte-exact CDX
response before parsing, or use a minimal local parser that retains raw fields
alongside typed values. Malformed rows should at least be preserved and
reported as unresolved rather than silently disappearing from completeness
accounting.

Keep the solution small. Replacing the entire Wayback client is not justified
unless the reviewer first demonstrates that a narrow raw-response hook cannot
work.

## Highest-priority performance work

### 3. Separate the 8 requests/second limit from in-flight concurrency

The current bounded worker pool conflates two different controls:

- how many requests may start per second; and
- how many slow requests may remain in flight.

The installed Wayback library already has a process-wide default memento rate
limiter that spaces starts at 8 requests/second. However, the default Fetch
pool also has only eight workers. If a response takes two seconds, eight
workers can sustain only about four requests/second even though the start-rate
budget is eight.

The desired model is a central download scheduler:

1. queue the captures that truly require playback;
2. release starts smoothly at no more than 8/second (roughly one every 125 ms,
   not a burst of eight on each second boundary);
3. allow a separately bounded number of requests to remain in flight;
4. put retryable work into a delayed priority queue instead of sleeping while
   holding a download slot;
5. make `Retry-After`/429 capable of closing a global gate, rather than letting
   every worker back off independently;
6. prefer first attempts over due retries unless evidence supports another
   fairness policy;
7. apply backpressure so increased concurrency cannot retain an unbounded
   number of large response bodies in memory.

Do not enqueue every selected CDX row naively. That would destroy the current
digest savings by starting duplicate downloads before a representative
finishes. For each normalized URL and valid non-redirect CDX digest, schedule
one representative candidate; if it fails, schedule the next candidate.
Redirects still require individual playback because the payload digest does
not validate `Location` or other HTTP headers.

Before redesigning, run a KISS experiment with 16, 24, and 32 workers. The
existing shared 8/second limiter should make that safe and will reveal the
in-flight concurrency needed to saturate the allowed start rate. This is a
benchmark, not necessarily the final architecture: sleeping retries still
consume those worker slots.

### 4. Add phase and request telemetry before claiming the bottleneck

The original nclr.org run took 788.1 minutes for 281,374 selected captures:

```text
195,068 responses, 84,087 revisits, 2,218 failed
```

The log records 2,432 scheduled retries totaling 64,421 worker-seconds of
requested backoff. Even with perfect eight-way overlap, that consumes at least
about 2.2 hours of worker capacity. The log also shows periods of widespread
503 and connection-refused responses, so "network time" includes server
outages and retry policy, not merely transferring bytes.

Add inexpensive aggregate metrics, preferably to `query.json` or a separate
machine-readable run summary:

- CDX search duration and request count;
- queue wait, rate-limit wait, connect/first-byte/body duration;
- requests started/completed per minute and peak in-flight count;
- response bytes and throughput;
- attempts and scheduled delay by failure/status category;
- time spent writing/copying/validating WARCs;
- existing-WARC inventory time;
- replay-index generation time;
- redirect-report time.

Avoid per-request durable telemetry unless sampled or aggregated; the old log
already had 113,756 lines.

### 5. Revisit WARC allocation and full-tree rescans

The old nclr.org collection has 51,458 WARC files totaling about 1.24 GB:

```text
average WARC size: 24,106 bytes
WARCs smaller than 4 KiB: 5,438
```

That shape is locally expensive even when network playback dominates:

- initial creation opens, compresses, validates, and publishes tens of
  thousands of tiny files;
- a merge inventories every existing WARC before scheduling builds;
- affected WARCs copy their complete baseline;
- every finalization rescans the full tree to regenerate replay CDXJ;
- filesystem metadata and backup costs are amplified.

Navigator relies on CDXJ filenames, not on one-WARC-per-resource, so larger
size-bounded or count-bounded shards are technically possible. Propose the
simplest deterministic sharding that preserves atomic replacement and limits
rewrite amplification. Measure current inventory/index time first, and keep
this separate from the download scheduler unless one design truly requires
the other.

### 6. Make large CDX searches resumable or partitioned

`search_captures()` materializes the entire result. If a paginated search is
rate-limited, its retry discards the partial result and starts the full query
again. A late failure in a 281,000-row query can therefore repeat substantial
IA work.

Investigate deterministic date partitions or durable resume keys. Preserve a
byte-exact source response as required by correctness item 2, detect duplicate
boundary rows by logical identity, and avoid adding a general database merely
to checkpoint one query.

## Failure recovery and completeness still to assess

### 7. Re-run the nclr.org failure analysis on the clean-slate format

The old log predates the new identity and accounting code. Its detail lines
break down approximately as follows:

| Old detail | Count |
| --- | ---: |
| Generic "Memento ... could not be played" | 1,849 |
| "has no mementos and was never archived" | 269 |
| Repeated truncation | 71 |
| Unexpected empty body | 18 |
| CDX 404 / playback 200 status mismatch | 8 |
| Robots denial | 4 |

These total 2,219 while the old headline said 2,218, which was the accounting
bug already fixed. Do not use the discrepancy as evidence of a current bug.

On a clean subset or a carefully bounded rerun, determine how many failures
are now:

- recovered locally through a same-URL CDX digest;
- exact-playback timestamp, URL, or status mismatches;
- unavailable playback despite a live CDX row;
- corrupt/truncated stored content;
- blocked by IA policy;
- malformed raw CDX rows that never reached selection.

The current exact-only policy is intentional. Do not substitute a capture days
or months away. For non-redirects, payload-digest recovery is the approved
local substitute. Redirect failures cannot use it because the digest does not
cover `Location`.

### 8. Research an exact raw-record retrieval path

As a fresh alternative, determine whether IA can expose the indexed WARC
filename, byte offset, and compressed length for public CDX results, and
whether authorized HTTP range retrieval of that exact WARC member is stable
and permitted. If available, it might avoid the CDX-versus-replay mismatch,
preserve original headers, and reduce Wayback replay work. It may also be
unavailable for public indexes or impose different rate limits.

Treat this as a feasibility study. Do not implement against undocumented
internal endpoints or assume it is faster without a representative benchmark.

### 9. Produce a structured failure artifact

The human log is not an ideal completeness ledger. Consider a versioned
`failures.json` beside `redirects.json`, derived from final outcomes and keyed
by capture identity. It should distinguish playback failure, exact metadata
mismatch, malformed CDX, digest ambiguity, corruption, and policy blocks, and
record whether a failure was recovered locally.

This is useful only if compact and deterministic. Do not create a second event
log or duplicate all console text.

## Deferred or lower-value work

### Whole-run staging

Per-WARC publication is atomic, but the whole collection is not transactional.
A crash or fatal worker error can leave some new WARCs published before replay
index and coverage finalization. A subsequent run should repair this, and the
user explicitly deferred staging/folder changes. Keep it as a reliability
project, not a prerequisite for the scheduler.

### Coverage semantics after partial success

`collection.json` is currently saved even when some captures fail. Under the
current implementation this is not a completeness bug: the manifest is only a
query-window envelope, and every later merge re-queries the entire union
window. It must never be treated as proof that every capture succeeded. If a
future optimization skips covered ranges, redesign the manifest first or
persist incomplete identities.

### Cross-URL digest deduplication

This remains conceptually possible but is not a major nclr.org opportunity.
Offline counts from its saved CDX show:

```text
unique non-redirect (URL key, digest) pairs: 196,321
unique non-redirect digests collection-wide: 194,626
maximum additional cross-URL savings: 1,695 requests (<1%)
```

By contrast, same-URL digest reuse avoided roughly 84,000 requests. Do not add
cross-WARC reference complexity for this collection without evidence from
other representative sites.

### Optional Navigator redirect page

Fetch now retains redirect status and `Location` and emits `redirects.json`.
A custom Navigator page saying "redirected to ..." was explicitly left for a
later pass. It is a UX enhancement, not a Fetch correctness blocker.

## Suggested review order

1. Reproduce and resolve the statusless-identity idempotence problem.
2. Decide how to preserve raw CDX bytes and account for malformed rows.
3. Add minimal aggregate timing/request instrumentation.
4. Benchmark higher in-flight concurrency under the existing shared 8/second
   limiter.
5. Design the central rate-limited queue and delayed retry scheduler using the
   benchmark results.
6. Measure merge inventory, WARC publication, and replay indexing on the old
   51,458-file collection; then assess sharding.
7. Re-run a bounded failure sample and evaluate exact raw-record retrieval.
8. Revisit structured failure reporting, whole-run staging, and Navigator UX.

For each recommendation, state the expected correctness benefit or measured
wall-time reduction. Reject changes that merely move code around, add flags
without an operator need, or optimize a component that measurements show is
minor.
