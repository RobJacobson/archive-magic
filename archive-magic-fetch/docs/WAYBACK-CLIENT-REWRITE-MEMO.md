# Wayback Client Rewrite Decision Memo

**Status:** Approved rewrite direction; implementation pending

**Date:** 2026-07-23

**Purpose:** Explain why `archive-magic-fetch` should replace `cdx_toolkit` and
its custom Internet Archive playback pipeline with the high-level `wayback`
library, and compare the existing implementation with the proposed
implementation for every major behavior.

## 1. Executive summary

`archive-magic-fetch` currently uses:

- `cdx_toolkit` to query the Internet Archive CDX API;
- `cdx_toolkit`'s host-wide request pacing state to throttle playback;
- a custom `requests` playback client to retrieve exact-timestamp captures;
- custom logic to distinguish playback compression from archived HTTP content
  encoding;
- CDX source-digest verification before semantic content normalization;
- custom detection of Wayback substitutions and canonical URL redirects; and
- `warcio` to write final WARC response and revisit records.

This design was motivated partly by possible future support for multiple CDX
archives. The implemented product, however, supports only the Internet Archive.
Its playback, verification, and redirect logic is already specific to the
Wayback Machine, so `cdx_toolkit` does not currently provide meaningful
cross-archive portability.

The rewrite should:

1. Replace `cdx_toolkit` with pinned `wayback==0.5.1`.
2. Use `WaybackClient.search()` for capture discovery.
3. Use `WaybackClient.get_memento()` in original mode for retrieval.
4. Accept the capture URL and timestamp normalization performed by `wayback`.
5. Treat `Memento.content` as the semantic payload to write.
6. Stop attempting to validate playback bytes against the CDX digest.
7. Retain CDX digests only as post-success retrieval-skipping hints.
8. Preserve genuine historical redirects instead of omitting canonical aliases.
9. Keep Archive Magic's WARC response/revisit writing and semantic-payload
   deduplication.
10. Keep all implementation in the existing
    `src/archive_magic_fetch/` package, without a new service, source-adapter
    hierarchy, generic client interface, or separate project.

This is a simplification, not merely a dependency swap. Much of the current
`retrieval.py` and its corresponding tests should be deleted because the
`wayback` library already owns Wayback discovery, exact playback selection,
redirect interpretation, retries, and endpoint-specific rate limiting.

## 2. Relationship to existing documents

This memo records the decision that led to the implemented rewrite.
`ARCHITECTURE-FETCH.md` now describes the current accepted architecture and
controls if it differs from this historical implementation memo. The rewrite
superseded the earlier policies concerning:

- `cdx_toolkit`;
- literal CDX URL authority;
- explicit default-port preservation;
- raw playback streaming;
- CDX source-digest verification;
- manual content-encoding reconstruction;
- Wayback substitution detection;
- canonical alias redirect omission; and
- host-wide six-second pacing.

The earlier WARC/CDX deduplication research memo was removed with the
deduplication implementation.

## 3. Product contract after the rewrite

Archive Magic will export a semantically replayable reconstruction of captures
that the Wayback Machine can play in original mode.

It will not claim that playback bytes are byte-identical to the original source
WARC member stored by the Internet Archive. The public Wayback playback service
may change content encoding between storage and delivery. The `wayback`
documentation explicitly warns that the CDX digest is generally based on the
stored representation and is not suitable for validating bytes returned by
`get_memento()`.

The durable invariants are:

- The user-selected URL pattern and time range are sent to Wayback search.
- Search results are fully iterated through the library's resume-key mechanism.
- Captures are grouped by Wayback CDX `urlkey`.
- Captures within each group are written in timestamp order.
- `get_memento()` is called in original mode with exact selection.
- Historical redirects are returned as redirects rather than followed.
- The WARC target URL and timestamp come from the playable capture exposed by
  the `wayback` client.
- The WARC payload digest covers the exact semantic bytes written by Archive
  Magic.
- Later payload/status duplicates are represented as WARC revisit records.
- A repeated CDX digest/status can avoid another download only after an earlier
  occurrence was retrieved successfully.
- Output paths, exclusive file creation, WARC 1.0, and the CLI surface remain
  unchanged unless implementation evidence requires a separate decision.

## 4. Why `wayback` is the better dependency

The `wayback` package is maintained by the Environmental Data and Governance
Initiative, not by the Internet Archive itself. It is nevertheless designed
specifically for the Wayback Machine and is actively updated using guidance
from Internet Archive staff. The Internet Archive's official
`internetarchive` package focuses on archive.org items and collections rather
than capture search and playback.

Compared with `cdx_toolkit`, `wayback` provides the behavior this project
actually needs:

- a typed `CdxRecord`;
- automatic complete resume-key iteration;
- a high-level `Memento` response model;
- original versus rewritten playback modes;
- exact-capture selection;
- explicit control over historical redirect following;
- Wayback-specific error types;
- connection pooling;
- retry policy;
- `Retry-After` parsing;
- separate CDX and memento rate limits; and
- thread-safe, shareable rate limiters for future bounded concurrency.

The current `cdx_toolkit` integration provides one substantial feature:
cross-source CDX compatibility. Archive Magic does not expose another source,
and Common Crawl retrieval would require source-WARC byte ranges rather than
Wayback playback. A future Common Crawl implementation should therefore be a
separate concrete implementation, added only when requested.

## 5. Major change comparisons

### 5.1 Runtime dependencies

#### Existing

```toml
dependencies = [
    "cdx_toolkit==0.9.39",
    "requests==2.34.2",
    "warcio==1.8.1",
]
```

Application code imports `requests` directly and imports private pacing helpers
from `cdx_toolkit.myrequests`.

#### Proposed

```toml
dependencies = [
    "wayback==0.5.1",
    "warcio==1.8.1",
]
```

Remove the direct `requests` dependency if no application module imports it
after the rewrite. `wayback` owns its own Requests dependency.

Pin `wayback` because it is pre-1.0 and its public API is still evolving.
Archive Magic should use the current, non-deprecated 0.5.1 names so a later
upgrade is deliberate and reviewable.

### 5.2 Client lifetime and ownership

#### Existing

`discovery.py` constructs a `CDXFetcher` internally. `retrieval.py` performs
module-level `requests.get()` calls. There is no explicit shared session
lifetime owned by the CLI.

```python
fetcher = cdx_toolkit.CDXFetcher(source="ia")
captures = list(fetcher.iter(...))
```

```python
response = requests.get(..., stream=True)
```

#### Proposed

`cli.py` creates one configured `WaybackSession` and `WaybackClient`, passes the
client to discovery and export, and closes it after the command.

Illustrative shape:

```python
session = WaybackSession(
    user_agent="archive-magic-fetch/0.1.0 (+project-url)",
)

with WaybackClient(session) as client:
    captures = discover(client, url_pattern, date_start, date_end)
    export_all(capture_groups, output_paths, client)
```

This provides one connection pool and one coherent set of rate limiters for the
entire job. It does not require a new Archive Magic client class.

### 5.3 CDX discovery

#### Existing

```python
fetcher = cdx_toolkit.CDXFetcher(source="ia")
return list(
    fetcher.iter(
        url_pattern,
        from_ts=date_start,
        to=date_end,
    )
)
```

`cdx_toolkit.iter()` uses numbered CDX pages. It applies the same host-wide
pacing used for playback. Its returned capture objects are mutable mapping-like
objects with source-specific behavior attached.

#### Proposed

```python
return list(
    client.search(
        url_pattern,
        from_date=date_start,
        to_date=date_end,
        resolve_revisits=False,
    )
)
```

Use the library defaults for:

- `limit=1000`, which is a per-request size rather than a total result limit;
- `skip_malformed_results=True`;
- no filter; and
- no collapse.

Explicitly set `resolve_revisits=False`. Internet Archive has described
server-side revisit resolution as unreliable and expensive, and Archive Magic
already handles payload reuse.

`WaybackClient.search()` automatically continues with resume keys and includes
recent captures that numbered-page mode may omit.

The CLI's existing partial numeric date strings can continue to be passed
through. The current `wayback` implementation sends string values unchanged.

### 5.4 Capture representation

#### Existing

Capture data is accessed as a mutable mapping:

```python
capture["urlkey"]
capture["url"]
capture["timestamp"]
capture.get("status")
capture.get("digest")
```

Tests use fake `CaptureObject`-like dictionaries.

#### Proposed

Use `wayback.CdxRecord` directly:

```python
capture.urlkey
capture.original
capture.timestamp
capture.statuscode
capture.digest
capture.mimetype
capture.length
```

Do not add a local capture dataclass, mapping adapter, abstract source record,
or compatibility wrapper unless implementation demonstrates a concrete need.
Using the upstream value object reduces translation code and makes the Wayback
dependency boundary explicit.

Tests should construct real `CdxRecord` values or small fakes with the same
public attributes.

### 5.5 URL and timestamp policy

#### Existing

The literal selected CDX URL is treated as authoritative, except that fragments
and bare empty queries are removed. Explicit default ports remain part of the
WARC target identity:

```text
http://example.com/
http://example.com:80/
```

The literal 14-digit CDX timestamp is retained and converted to WARC date form.

#### Proposed

Accept the URL and timestamp supplied by `CdxRecord` and `Memento`.

`wayback` removes redundant `:80` from HTTP and `:443` from HTTPS. This is
desirable simplification: those spellings identify the same effective HTTP
origin and provide no useful distinction for capture search, grouping, or
replay. Non-default ports remain distinct.

Allow the library to repair rare malformed timestamps that it knows how to
interpret. Archive Magic should represent the capture Wayback can play, not
preserve malformed CDX syntax as an independent product requirement.

Remove local fragment, bare-query, and exact-default-port policy unless a real
Wayback result demonstrates a remaining playback problem. Do not retain
normalization code solely because the old architecture specified it.

### 5.6 Playback retrieval

#### Existing

`retrieval.py` constructs the exact playback URL, performs raw streaming, and
disables urllib3 decoding:

```python
playback_url = f"{playback_base}/{timestamp}id_/{quote(url)}"
response = requests.get(
    playback_url,
    allow_redirects=False,
    stream=True,
)
raw_payload = response.raw.read(decode_content=False)
```

The module then interprets playback headers, detects substitutions, compares
raw and decoded candidates, decodes archived encodings, repairs HTTP headers,
and constructs a WARC response.

#### Proposed

Use the public high-level API:

```python
memento = client.get_memento(
    capture,
    mode=Mode.original,
    exact=True,
    follow_redirects=False,
)
payload = memento.content
```

Meanings:

- `Mode.original` avoids toolbar injection and URL rewriting.
- `exact=True` prevents accepting a different nearby capture.
- `follow_redirects=False` returns the selected historical 3xx response rather
  than navigating to its destination.
- `Memento` exposes the captured URL, timestamp, status, cleaned historical
  headers, playback URL, and semantic body.

Delete direct playback URL construction and raw Requests handling.

### 5.7 Content encoding and source verification

#### Existing

The current code tries to decide whether gzip/deflate is:

- an archived origin `Content-Encoding`;
- Wayback transfer compression; or
- both.

It hashes raw and playback-decoded candidates and requires one to match the CDX
digest. It then decodes supported archived content encodings into semantic
payload bytes. Unsupported encodings are skipped.

This logic was intended both to detect substituted content and to make encoded
and identity representations of the same semantic body deduplicate.

#### Proposed

Treat `Memento.content` as the semantic payload and do not compare it to the CDX
digest.

The `wayback` documentation explains why: a CDX digest describes the response
body as stored, while playback can transform its content encoding. A body
stored using Brotli can be delivered using gzip, so the bytes returned by
`get_memento()` are not expected to match the CDX digest.

The `wayback` library also contains a targeted workaround for Wayback's
malformed duplicate gzip headers. Requests decodes normal transfer compression
when `Memento.content` is read.

Consequences:

- Delete `SourceDigestMismatch`.
- Delete raw-versus-decoded source hashing.
- Delete custom gzip, x-gzip, and deflate decoding.
- Delete `source_verified` from retrieval results.
- Do not skip a successfully retrieved memento because its semantic body does
  not match a storage-representation digest.

If exact source-WARC fixity becomes a product requirement, implement direct
source-WARC retrieval as a separate future feature. Wayback playback is not a
byte-exact source-WARC interface.

### 5.8 HTTP header reconstruction

#### Existing

Archive Magic extracts `x-archive-orig-*` headers itself, decides whether a
direct `Content-Encoding` is historical, repairs `Location`, conditionally
removes representation validators, and always replaces `Content-Length`.

#### Proposed

Start from `memento.headers`, which contains the library's cleaned historical
headers, then apply a small deterministic WARC-consistency filter.

Always remove:

```text
Content-Encoding
Transfer-Encoding
Content-Length
ETag
Content-MD5
Content-Digest
Digest
Repr-Digest
Content-Range
```

Always add:

```text
Content-Length: <len(memento.content)>
```

Retain useful semantic and behavioral headers such as:

- `Content-Type`;
- `Location`;
- cache controls;
- language;
- dates; and
- other headers that do not describe a possibly transformed representation.

Unconditional removal is simpler and safer because Archive Magic no longer
tries to prove whether playback transformed the stored representation.

### 5.9 CDX digest and semantic deduplication

#### Existing

Two maps are maintained:

```text
(verified CDX source digest, known status)
    -> normalized payload digest and canonical response

(normalized payload digest, known status)
    -> canonical response
```

A CDX signature is trusted only after source-digest verification.

#### Proposed

Retain the same two-stage optimization, but replace "verified against stored
bytes" with "associated after successful exact memento retrieval."

```text
(CDX digest, known status)
    -> semantic payload digest and canonical response
    only after a successful first retrieval

(semantic payload digest, response status)
    -> canonical response
```

For each capture:

1. If its CDX digest/status already maps to a successfully retrieved canonical
   response, write a revisit without another playback request.
2. Otherwise call `get_memento()`.
3. Hash `memento.content` using the WARC payload digest algorithm.
4. If the semantic digest/status already has a canonical response, write a
   revisit.
5. Otherwise write a full response.
6. After the successful response or revisit decision, associate the capture's
   usable CDX digest/status with that canonical response.

This preserves the important bandwidth optimization. It also allows two
different stored encodings with different CDX digests to converge on one
semantic canonical payload after both have been fetched.

For a CDX revisit with no status, reuse a known canonical source digest only
after such a canonical exists. Otherwise retrieve the memento and use the
returned response status.

### 5.10 Redirects and playback substitutions

#### Existing

Archive Magic:

- detects `X-Archive-Redirect-Reason`;
- parses Memento `Link` headers;
- rejects playback-generated substitutions;
- omits historical redirects that differ only by scheme, one `www.` label, or
  matching default port; and
- preserves redirects that change domain, path, query, or non-default port.

This requires substantial special-case code and tests.

#### Proposed

Delegate exact-capture and playback-fallback interpretation to
`WaybackClient.get_memento()`.

With `exact=True`, a non-exact fallback becomes a Wayback exception rather than
a successful origin response. With `follow_redirects=False`, a genuine
historical redirect is returned as the selected memento.

Preserve all genuine historical 3xx responses, including HTTP-to-HTTPS and
`www` canonical redirects. They are small records, are part of capture history,
and do not justify a custom omission classifier.

Consequences:

- Delete `PlaybackSubstitution`.
- Delete custom Memento `Link` parsing.
- Delete canonical alias identity and redirect classifiers.
- Delete omitted-redirect signature state and summary output.
- Stop treating default-port spelling as a special redirect policy.

### 5.11 Request pacing and retries

#### Existing

`cdx_toolkit` maintains one in-process delay keyed by
`web.archive.org`. Its default interval is six seconds for both CDX and
playback. The custom retriever copies its exponential retry behavior and does
not honor `Retry-After`.

#### Proposed

Use the current `WaybackSession` defaults:

```text
CDX search: 0.4 calls/second
Memento playback: 8 calls/second
```

The library:

- distinguishes rate limits by endpoint path;
- pools connections;
- performs bounded retries for transient Wayback failures;
- parses `Retry-After`;
- supplies a 60-second recommendation for `429` without that header; and
- raises `RateLimitError` rather than silently retrying indefinitely.

Archive Magic should add one small `RateLimitError` policy:

1. Pause for `error.retry_after` or 60 seconds.
2. Retry the same operation once.
3. If rate-limited again, stop the job with a clear fatal error rather than
   continuing through captures and causing more `429` responses.

Keep export serial during this rewrite. Measure first. Bounded concurrency is a
separate future change and must use shared rate limiters if implemented.

### 5.12 Error handling

#### Existing

Custom exceptions distinguish:

- capture retrieval failures;
- source digest mismatches; and
- playback substitutions.

Most remote failures warn, skip, and continue.

#### Proposed

Map public `wayback` exceptions into the existing user-facing policy:

- `NoMementoError`, `MementoPlaybackError`, `BlockedByRobotsError`, and
  `BlockedSiteError`: warn, skip that capture, and continue.
- exhausted `WaybackRetryError`: warn, skip that capture, and continue.
- `RateLimitError`: pause and retry once; a second rate limit is fatal for the
  job.
- malformed local state, file failures, WARC serialization failures, and
  programming errors: fatal.

Do not preserve old exception classes solely to avoid changing tests.

### 5.13 WARC construction

#### Existing

`retrieval.py` constructs a `warcio` response after source verification and
normalization. `export.py` prepares target/date identity and decides response
versus revisit. `warc.py` serializes final records.

#### Proposed

Archive Magic still owns final WARC construction because the product requires
its semantic payload digest and response/revisit policy.

Build the response from:

```text
WARC-Target-URI      memento.url
WARC-Date            memento.timestamp
WARC-Source-URI      memento.memento_url
HTTP status          memento.status_code plus a standard reason phrase
HTTP headers         filtered memento.headers
HTTP body            memento.content
WARC-Payload-Digest  digest of memento.content
```

Keep:

- WARC 1.0;
- gzip output;
- initial `warcinfo`;
- exclusive target creation;
- canonical response references;
- revisit writing;
- cross-target reference fields currently needed by the chosen grouping
  policy; and
- one WARC per CDX URL key.

### 5.14 Console behavior

Keep the small existing progress surface:

```text
Starting https://example.com/
Downloaded 20200101123000 [a1b2c3d4]
WARNING skipped ...
```

Remove:

```text
Omitted N canonical URL redirects
```

because historical redirects will no longer be omitted.

Do not add structured logging, progress bars, or verbosity configuration as
part of this rewrite.

## 6. File-by-file impact

| File | Existing responsibility | Rewrite |
| --- | --- | --- |
| `pyproject.toml` | Pins `cdx_toolkit`, `requests`, and `warcio` | Pin `wayback` and `warcio`; remove direct `requests` if unused |
| `uv.lock` | Locks the current dependency graph | Regenerate after dependency change |
| `cli.py` | Parses CLI and invokes module-level discovery/export | Own one `WaybackSession`/`WaybackClient` context and pass the client down |
| `discovery.py` | `CDXFetcher`, URL cleanup, row collapse, grouping | `WaybackClient.search`, `CdxRecord` grouping, minimal duplicate handling |
| `paths.py` | Maps URL keys to safe output paths | Expected to remain unchanged |
| `retrieval.py` | Raw playback, retries, encoding inference, source verification, reconstruction | Small `get_memento`-to-WARC response conversion and header filter |
| `export.py` | Mapping-based capture loop, source verification state, alias omission, dedup | Attribute-based `CdxRecord` loop and successful-fetch/semantic dedup |
| `warc.py` | WARC response/revisit helpers | Mostly unchanged; may absorb response construction if clearer |
| `tests/test_discovery.py` | Fake `CDXFetcher` and mapping captures | Fake `WaybackClient.search` and `CdxRecord` values |
| `tests/test_retrieval.py` | Raw/decoded transport, substitution, digest verification | Memento conversion, headers, status, semantic digest, Wayback errors |
| `tests/test_export.py` | Verified-source and alias/substitution policy | Successful-first-fetch mapping, semantic dedup, historical redirects |
| `tests/test_paths.py` | Safe URL-key paths | Expected to remain unchanged |

Do not introduce `core/`, `application/`, `clients/`, `adapters/`, `services/`,
or `models/` directories. A new source file is not required by this decision.
Prefer revising the existing narrow modules.

## 7. Test migration

### 7.1 Delete or replace obsolete tests

Remove tests whose only purpose is the superseded design:

- raw versus transfer-decoded source candidates;
- CDX source-digest match and mismatch;
- custom gzip/x-gzip/deflate decoding;
- unsupported content-encoding skip behavior;
- direct playback URL construction;
- `get_retries` and `update_next_fetch`;
- custom retry status loops;
- `X-Archive-Redirect-Reason`;
- custom Memento `Link` substitution parsing;
- exact explicit-default-port preservation;
- canonical scheme/`www`/default-port redirect omission;
- repeated omitted-redirect signature reuse; and
- omitted canonical redirect console summaries.

### 7.2 Preserve and adapt valuable tests

Retain coverage for:

- CLI date defaults and explicit bounds;
- successful empty discovery;
- complete discovery failure being fatal;
- grouping by `urlkey`;
- timestamp ordering;
- safe deterministic output paths;
- path collision and existing-output preflight;
- response then revisit ordering;
- same CDX digest/status avoiding a second fetch after success;
- different CDX digests with identical semantic content becoming revisits;
- missing CDX digest forcing retrieval;
- retrieval failures warning and continuing;
- an all-skipped group producing no WARC;
- filesystem and WARC failures remaining fatal; and
- output parsing through `warcio.ArchiveIterator`.

### 7.3 Add rewrite-specific tests

Add deterministic tests that assert:

1. `discover()` calls `WaybackClient.search()` with the URL pattern, explicit
   bounds, and `resolve_revisits=False`.
2. Default-port variants use the normalized `CdxRecord.original` value.
3. Captures are consumed as `CdxRecord` attributes rather than mutable mapping
   behavior.
4. Retrieval calls `get_memento()` with `Mode.original`, `exact=True`, and
   `follow_redirects=False`.
5. The response uses `Memento.url`, `timestamp`, `status_code`,
   `memento_url`, headers, and content.
6. Representation-dependent headers are removed and `Content-Length` is
   replaced.
7. The WARC payload digest matches `Memento.content`.
8. Genuine historical 3xx responses are written, including scheme/`www`
   canonical redirects.
9. A repeated CDX digest/status is not trusted until the first retrieval
   succeeds.
10. A failed first occurrence does not seed either deduplication map.
11. Wayback playback/blocked/no-memento errors warn and continue.
12. A `RateLimitError` pauses and retries the same operation once.
13. A second consecutive `RateLimitError` stops the job.
14. CDX and memento rate limits are not reimplemented in application code.

Routine tests must remain deterministic and offline. A small opt-in manual
Internet Archive smoke test may be run after the deterministic suite passes.

## 8. Expected simplifications

The rewrite should remove substantially more code than it adds.

Expected deletions include:

- private `cdx_toolkit` pacing imports;
- custom raw request loop;
- raw payload access;
- gzip/deflate helpers;
- source-digest mismatch reporting;
- source-verification state;
- playback substitution exceptions;
- Memento `Link` parsing;
- manual Wayback `Location` decoding;
- canonical alias identity and redirect classification;
- omitted-redirect state; and
- many transport-specific unit fixtures.

Do not preserve old internal APIs for compatibility. `archive-magic-fetch` is
an application, and these helpers are not a public library contract.

## 9. Risks and mitigations

### `wayback` is pre-1.0

Mitigation: pin 0.5.1, use non-deprecated names, and upgrade only with tests.

### Playback is not source-WARC fixity

This is an explicit product decision, not an unnoticed limitation. The WARC
records Archive Magic writes remain internally fixable through their own
payload digests.

### Header transformations are not always observable

Mitigation: remove representation-specific framing and validators
unconditionally, then describe only the semantic body actually written.

### Default-port spelling is lost

This is accepted and desirable. Matching default ports are semantically
redundant. Non-default ports remain distinct.

### Malformed search rows may be skipped

This is accepted through `skip_malformed_results=True`. An unplayable malformed
URL does not serve the product goal.

### Rate limiting can affect a long job

Mitigation: use the upstream endpoint limits, pause once on `429`, and stop
rather than hammering after a second `429`.

### High-level client behavior might differ from current special cases

Mitigation: preserve representative deterministic fixtures at the Memento
boundary and run one small manual smoke test after implementation.

## 10. Non-goals

This rewrite does not add:

- Common Crawl support;
- a generic archive interface;
- a source adapter hierarchy;
- direct Internet Archive source-WARC retrieval;
- concurrency;
- resume/checkpoint support;
- atomic staging;
- output-root configuration;
- CDXJ or manifest generation;
- WARC 1.1;
- page dependency discovery;
- structured logging; or
- a new CLI option for rate limits.

## 11. Acceptance criteria

The rewrite is complete when:

1. `cdx_toolkit` is absent from runtime code and dependencies.
2. No application code performs raw Wayback playback through `requests`.
3. One `WaybackClient` context owns search and playback for the command.
4. Search uses explicit bounds and `resolve_revisits=False`.
5. Retrieval uses original mode, exact selection, and no historical redirect
   following.
6. Default ports follow `wayback` normalization.
7. Genuine historical redirects are written rather than omitted.
8. CDX digests are never used to reject retrieved Memento content.
9. CDX digest/status reuse begins only after a successful first retrieval.
10. Semantic duplicate payloads produce response/revisit output without
    duplicate bodies.
11. WARC payload digests match the bytes actually written.
12. Representation-dependent HTTP headers describe the written body
    consistently.
13. Wayback errors are mapped to the agreed warning/fatal behavior.
14. `RateLimitError` pauses once and becomes fatal if repeated.
15. All deterministic tests pass offline.
16. `uv lock --check` passes.
17. `uv run archive-magic-fetch --help` preserves the current CLI.
18. A small manual run produces parseable WARC 1.0 output.
19. No speculative archive abstraction or concurrency is introduced.

## 12. References

- [`wayback` documentation](https://wayback.readthedocs.io/en/stable/)
- [`WaybackClient`, `WaybackSession`, and rate-limit source](https://wayback.readthedocs.io/en/stable/_modules/wayback/_client.html)
- [`CdxRecord` digest and `Memento` documentation](https://wayback.readthedocs.io/en/stable/usage.html)
- [`wayback` 0.5.1 release history](https://wayback.readthedocs.io/en/stable/release-history.html)
- [Internet Archive automated-access guidance](https://archive.org/developers/bots.html)
- [`cdx_toolkit` 0.9.39 source](https://github.com/commoncrawl/cdx_toolkit/tree/0.9.39)
- [`warcio` documentation](https://warcio.readthedocs.io/en/latest/)
