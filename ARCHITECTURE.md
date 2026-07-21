# Archive Magic Architecture

**Status:** Current architecture plan · **Updated:** July 21, 2026 · **Purpose:** Define the four independently implemented stages, the initial WARC 1.0 one-URL-per-file profile, and the file contract between Fetch and Replay

## 1. Architectural decision

Archive Magic downloads selected captures from public CDX-based web archives and publishes portable collections of WARC files. The initial implementation follows four separately accepted stages:

1. **Project foundation:** establish the top-level requirements, boundaries, licenses, and collection-file contract.
2. **Fetch core:** implement a separately testable Python pipeline inside `archive-magic-fetch` for CDX discovery, archived-payload retrieval, digest-based fetch planning, and WARC creation.
3. **Fetch application:** implement the CLI and collection transaction around the accepted Fetch core, including inventory, indexes, validation, manifests, and atomic publication.
4. **Replay:** implement or integrate replay as a separate project that consumes only completed collection files.

These are implementation stages, not one inseparable change. Each stage has its own responsibilities, tests, and acceptance gate. Stage 2 and Stage 3 live in the same Fetch project but remain distinct architectural layers: the core transforms public archive records into closed WARC files, while the application manages users and collection publication.

The dependency direction is:

```text
Stage 1: project foundation and collection contract
                   |
                   v
Stage 2: archive-magic-fetch core
         |
         +-- pinned stock cdx_toolkit for CDX discovery only
         +-- payload source adapters
         +-- WARC 1.0 writer
                   |
                   v
Stage 3: archive-magic-fetch application
                   |
                   | completed collection files only
                   v
Stage 4: archive-magic-replay
```

Archive Magic will not maintain a `cdx_toolkit` fork for the initial release. Fetch uses an exact tested revision of the unmodified upstream package and owns all Archive Magic payload retrieval, deduplication, and WARC-writing behavior.

## 2. Folder overview

The following source layout shows the intended boundaries across all four stages. Internal module names are illustrative and may be refined without changing ownership.

```text
archive-magic/
├── README.md
├── ARCHITECTURE.md
├── LICENSE
├── THIRD_PARTY_NOTICES.md
│
├── archive-magic-fetch/
│   ├── README.md
│   ├── pyproject.toml
│   ├── src/
│   │   └── archive_magic_fetch/
│   │       ├── core/                      Stage 2 Fetch core
│   │       │   ├── cdx/                   stock cdx_toolkit integration
│   │       │   ├── retrieval/             playback and public-WARC adapters
│   │       │   ├── export/                per-URL planning and WARC writing
│   │       │   └── models/                normalized records and outcomes
│   │       └── application/               Stage 3 CLI and collection lifecycle
│   └── tests/
│       ├── core/                          Stage 2 tests
│       └── application/                   Stage 3 tests
│
└── archive-magic-replay/
    ├── README.md
    ├── pyproject.toml
    ├── src/
    │   └── archive_magic_replay/          Stage 4 application
    └── tests/
```

The runtime archive root is separate from the source tree:

```text
<archive-root>/
├── staging/                               unpublished Stage 3 work
├── collections/                           immutable published collections
│   └── <slug>/                            Stage 3 output; Stage 4 input
├── runtime/                               ephemeral Stage 4 state
└── .locks/                                non-collection process locks
```

Fetch and Replay are sibling applications but share no runtime package, process, database, or staging state. Installing either application must not install the other.

## 3. Stage 1 requirement: project foundation

### 3.1 Purpose

Stage 1 establishes the product and file boundaries before Fetch or Replay behavior is implemented.

Stage 1 defines:

- The separation between Fetch core, Fetch application, and Replay.
- The supported public-archive source shape.
- The initial WARC 1.0 one-URL-per-file profile.
- The completed collection directory and manifest contract.
- Collection portability, immutability, validation, and publication invariants.
- Licensing and third-party attribution.
- Exact dependency-pinning policy.
- Acceptance criteria for the later stages.

Stage 1 does not implement CDX access, payload retrieval, WARC creation, the Fetch CLI, or Replay.

### 3.2 Completed collection contract

Fetch publishes a self-contained collection with this logical shape:

```text
collections/<slug>/
├── archive/
│   ├── <url-id-00000>.warc.gz
│   ├── <url-id-00001>.warc.gz
│   └── ...
├── indexes/
│   ├── <url-id-00000>.cdxj
│   ├── <url-id-00001>.cdxj
│   └── ...
└── metadata/
    ├── manifest.json
    └── source-inventory.jsonl
```

Each WARC and matching CDXJ represent exactly one exact target URL. The stable `url-id` filename mapping must be deterministic, collision-safe, portable, and recorded in both the inventory and manifest. The precise naming algorithm is a Stage 1 contract detail; raw URLs are not used directly as filesystem paths.

The collection directory is the only integration contract between Fetch and Replay. Copying the `<slug>/` directory to another machine or archive root must preserve its validity. Published metadata uses normalized relative paths and contains no dependency on Fetch staging paths.

The versioned manifest is the authoritative completion marker. It identifies every WARC, CDXJ, and inventory artifact and records its exact size and checksum. Only explicit final states such as `completed` and `completed_with_warnings` may be published. In-progress, cancelled, and failed work remains in staging, and a collection with no preserved captures is never published.

The source inventory is mandatory. It contains one durable outcome for every selected capture, including the complete raw CDX row, normalized fields, source digest, calculated digest for downloaded payloads, warnings, output WARC, and record references. Source null, empty, missing, and unknown values remain distinguishable.

Published collections are immutable. Updating, extending, repairing, merging, or changing the WARC profile produces a new collection or a future explicitly versioned operation; it does not mutate published WARC bytes or indexes.

### 3.3 Acceptance gate

Stage 1 is accepted when the repository boundaries, source model, WARC profile, versioned collection contract, licensing, and four implementation gates can be understood without relying on oral history.

## 4. Stage 2 requirement: Fetch core

### 4.1 Purpose and dependency policy

Stage 2 implements the source-to-WARC core inside `archive-magic-fetch`. It is ordinary Archive Magic application code, not a separate service, executable, repository, or published compatibility fork.

The core imports an exact tested release of stock [`commoncrawl/cdx_toolkit`](https://github.com/commoncrawl/cdx_toolkit) for CDX request construction, dialect normalization, and paged iteration. It does not use or modify the stock `cdxt warc` exporter. Archive Magic may submit isolated upstream bug fixes, but upstream acceptance is never a release dependency.

`cdx_toolkit` has one architectural role in the initial implementation: obtain CDX records. Fetch core owns the decisions and side effects that occur after discovery, including content retrieval and final WARC construction.

### 4.2 Supported public archive shape

Fetch supports a public archive when it provides:

1. A public CDX endpoint compatible with the configured stock `cdx_toolkit` integration; and
2. One of the following public content-retrieval mechanisms.

#### Wayback-compatible exact playback

The CDX row identifies a capture and an exact-timestamp playback request returns its reconstructed archived response:

```text
GET <cdx-endpoint>?url=<pattern>&...
GET <playback-base>/<timestamp>id_/<original-url>
```

Internet Archive is one instance of this source shape, but the Fetch core must not be IA-only.

#### Public source-WARC byte ranges

The CDX row supplies a source WARC filename, compressed offset, and compressed length. Fetch range-loads and parses the original record from publicly accessible storage:

```text
GET <public-warc-base>/<filename>
Range: bytes=<offset>-<offset+length-1>
```

Common Crawl is one instance of this source shape.

A CDX endpoint without publicly retrievable content is insufficient. Local WARC input, another organization's private object storage, and speculative archive-specific APIs remain outside the initial scope.

### 4.3 Core pipeline

The Fetch core has explicit internal steps:

```text
stock cdx_toolkit
    -> discover and normalize CDX records
    -> group records by exact target URL
    -> process each URL independently
         -> create a fresh per-URL digest map
         -> fetch unseen payloads through the source adapter
         -> verify downloaded payload digest
         -> create WARC 1.0 response or revisit records
         -> close that URL's WARC
    -> return closed WARC files and per-capture outcomes
```

The components remain separately testable even though they execute in one Python process:

- The CDX integration discovers records and preserves complete raw source rows.
- Source adapters retrieve archived responses through exact playback or public WARC ranges.
- The per-URL planner decides whether a capture requires retrieval.
- The digest verifier hashes downloaded payload bytes and enforces the source-digest policy.
- The record factory creates WARC 1.0 response and revisit records.
- The WARC writer owns record-at-a-time GZIP and one file per exact URL.
- The core orchestrator groups records, owns the per-URL map, and returns structured outcomes.

### 4.4 Initial selection policy

Only caller-supplied constraints may narrow discovery. Fetch core does not silently impose a result limit, digest collapse, status filter, or MIME filter. Archived redirects and origin 4xx/5xx responses are selected archival data, not Fetch failures.

Discovery preserves every selected capture and its original exact URL. CDX `urlkey` or SURT values may be used for ordering and source lookup but must not replace or merge exact target URLs for WARC grouping.

### 4.5 Initial WARC 1.0 one-URL-per-file profile

Each exact target URL is processed as an independent unit:

```python
for url, captures in captures_grouped_by_exact_url:
    seen = {}
    warc = open_new_warc_for(url)

    for capture in captures:
        expected = normalize(capture.digest)

        if expected is not None and expected in seen and can_build_revisit(capture):
            warc.write_revisit(capture, canonical=seen[expected])
            continue

        retrieved = retrieve_archived_response(capture)
        actual = calculate_payload_digest(retrieved.payload)

        if expected is not None and actual != expected:
            raise SourceDigestMismatch(capture, expected, actual)

        canonical = warc.write_response(capture, retrieved, actual)
        seen.setdefault(actual, canonical)

    warc.close()
```

The pseudocode expresses policy rather than a required API. The architectural rules are:

- One WARC file contains records for one exact target URL only.
- A new digest map is created when processing begins for that URL and discarded when its WARC closes.
- No digest or canonical-response state is retained across URLs.
- The map is keyed only by normalized payload digest because the surrounding processing unit already fixes the exact URL.
- The map value identifies the earlier canonical response needed to create and validate a revisit.
- The first occurrence of a digest for that URL is downloaded and written as a full response.
- A later capture with the same valid source digest becomes a revisit without downloading its payload when the CDX row contains enough response semantics to build a valid revisit.
- Every selected capture produces a response, revisit, or explicit unavailable outcome; deduplication never collapses the capture timeline.
- A missing or malformed source digest forces retrieval and a full response. Its calculated digest may seed the current URL's map for later captures, but the fetched capture is not retroactively changed into a revisit.
- If a duplicate redirect lacks the source metadata needed to preserve its redirect behavior, Fetch retrieves it and writes a full response.
- Request records are written only when a source supplies authentic request data. Fetch never invents an original request.

### 4.6 Digest trust and error policy

The source CDX digest is trusted for the decision to skip a previously seen payload within the same URL. Skipped duplicate payloads are not independently reverified because avoiding that retrieval is the purpose of the optimization.

Every downloaded payload is hashed from the bytes Fetch will write. The calculated digest is used in the WARC record and compared with a valid source digest. A mismatch is a source-integrity or representation error and aborts the collection; Fetch does not maintain a second actual-digest deduplication path or silently substitute a different canonical history.

This strict policy is intentionally simple. Each supported source adapter must demonstrate through deterministic and live tests that its retrieval representation normally hashes to the CDX digest. If a concrete archive legitimately transforms payloads, that archive requires an explicit adapter policy before support is claimed.

### 4.7 WARC file requirements

Each per-URL WARC must:

- Use WARC 1.0.
- Start with a `warcinfo` record.
- Use record-at-a-time GZIP.
- Preserve the selected capture's exact target URL and timestamp.
- Write the canonical response before any dependent revisit.
- Use the identical-payload-digest revisit profile.
- Contain no response or revisit records for another target URL.
- Be closed before its CDXJ is generated.
- Never be recompressed or modified after indexing.

The initial profile has no cross-URL WARC packing and no collection-wide WARC rollover target. A WARC may therefore be very small when a URL has few captures or large when one URL has extensive capture history. This file-count and file-size tradeoff is accepted for implementation simplicity.

### 4.8 Independent testing and acceptance gate

Stage 2 is tested without the Fetch CLI, publication layer, Replay, or pywb process. Deterministic fake servers and fixtures cover:

- Multiple CDX dialects and paging.
- Absence of hidden caps, collapse, status filters, and MIME filters.
- Exact-URL grouping without SURT-based merging.
- Wayback-compatible playback retrieval.
- Public source-WARC range retrieval.
- Source revisits whose canonical payload is outside the selected subset.
- Per-URL map reset and proof that identical cross-URL payloads remain separate responses.
- Same-URL response/revisit behavior.
- Missing, malformed, matching, and mismatched source digests.
- Redirects with and without indexed targets.
- Archived non-2xx responses.
- Authentic-only request-record behavior.
- Source pacing, bounded retries, and `Retry-After` using injected clocks and sleepers.
- WARC structure, payload digests, and self-contained revisit resolution.

Stage 2 is accepted when it can independently generate and validate one-URL WARC files for both supported retrieval mechanisms and a mismatch reliably fails rather than publishing ambiguous output.

## 5. Stage 3 requirement: Fetch application

### 5.1 Purpose

Stage 3 implements the Archive Magic-facing collection transaction around the accepted Fetch core.

The application owns:

- CLI arguments and configuration precedence.
- Public source presets and explicit public-source configuration.
- URL-pattern validation and collection naming.
- Archive-root locking and staging policy.
- Complete discovery before payload retrieval, followed by exact counts, review, and confirmation.
- Human-readable progress and stable machine-readable output.
- Durable source-inventory generation.
- Stable URL-to-filename identifiers.
- One sorted CDXJ per closed per-URL WARC.
- Collection-level validation and checksums.
- Versioned manifest creation.
- Atomic publication.
- Cancellation, cleanup, warnings, and exit categories.

### 5.2 Application data path

```text
user request
    |
    v
Fetch validates configuration and acquires the collection lock
    |
    v
Fetch core uses stock cdx_toolkit to discover captures
    |
    v
Fetch durably records the complete selected inventory
    |
    | exact counts, review, and confirmation
    v
Fetch core writes one closed WARC per exact URL
    |
    v
Fetch application creates matching CDXJ files, checksums, and manifest
    |
    v
Fetch validates and atomically publishes the collection
```

Discovery completes before payload retrieval begins. This gives exact progress denominators, makes confirmation meaningful, and creates the durable audit inventory. Export may stream URL groups and payloads from the staged inventory rather than holding the complete collection in memory.

### 5.3 Job and publication invariants

The initial release favors a small, safe lifecycle over resumability:

- Only one Fetch job operates in an archive root at a time, enforced by an operating-system-backed lock.
- An interrupted or failed job restarts from the beginning and does not reuse partial WARCs or digest maps.
- Stale staging work is never silently deleted.
- An existing published collection name causes a conflict; Fetch does not overwrite, merge, or invent a numbered replacement.
- Symlinks, path escapes, unexpected absolute paths, and unrelated staging directories are not followed or published.
- Published collections are immutable.

Publication occurs only after every WARC is closed, each matching CDXJ is generated from its final bytes, and all artifacts pass structural and semantic validation. Validation includes WARC parsing, digest agreement, same-URL response/revisit resolution, one preserved record per successful selected capture, inventory/count agreement, CDXJ ordering, relative filenames, exact compressed offset/length extraction, and one-to-one WARC/CDXJ pairing.

Fetch computes checksums, writes and rereads the versioned manifest, and atomically renames the whole collection directory into `collections/<slug>`. That rename is the publication commit point. Cleanup failure after a successful rename must not cause Fetch to misreport or roll back a valid collection.

### 5.4 Failure boundary

- Archived origin responses, including 4xx and 5xx, are preserved data.
- A definitively unavailable capture may become a recorded warning when other captures were preserved.
- Missing or malformed source digests are handled by retrieval and are not themselves fatal.
- A valid source digest that disagrees with the downloaded payload is fatal.
- Exhausted transient source failures abort publication.
- Invalid WARCs, unresolved revisits, inconsistent counts, invalid indexes, unsafe paths, or checksum failures abort publication.
- A job that preserves zero captures never publishes a collection.

A successful collection with warnings remains a valid `completed_with_warnings` collection. Ambiguous source failures, digest mismatches, and validation failures cannot be downgraded to warnings.

### 5.5 Independent testing and acceptance gate

Stage 3 tests application behavior without starting Replay and without duplicating the core's record-level suite. Tests cover CLI and configuration behavior, collection naming and conflicts, locking and staging, cancellation and cleanup, discovery confirmation, progress mapping, URL-ID collisions, inventory completeness, WARC/CDXJ pairing, exact byte-range validation, checksums, manifest invariants, safe paths, atomic publication, and proof that failed jobs leave no visible collection.

The main suite exercises the public Fetch interface against deterministic local CDX, playback, and source-WARC fixtures. Narrow live-source checks are opt-in, serial, non-blocking, and assert durable invariants rather than exact remote capture counts.

Stage 3 is accepted when Fetch independently creates a valid, portable one-URL-per-WARC collection for each supported retrieval mechanism and publishes it atomically. Replay is not part of this gate.

## 6. Stage 4 requirement: Replay

Replay remains a separate project and is not otherwise redesigned here. It consumes the completed collection contract established in Stage 1 and produced by Stage 3.

Replay must not import Fetch, share Fetch staging or locks, observe incomplete collections, or modify published files. It accepts portable collections rather than assuming Fetch ran on the same machine. It may consume both `completed` and `completed_with_warnings` collections after validating the supported manifest version and basic file safety.

Replay may use pywb and its own dependencies independently. Its implementation, deployment, interface, and test plan belong to the Replay project. Stage 4 begins only after Stage 3 has accepted fixtures and the collection contract is stable.

## 7. Responsibility matrix

| Requirement | Stage 1 foundation | Stage 2 Fetch core | Stage 3 Fetch app | Stage 4 Replay |
|---|---:|---:|---:|---:|
| Define product and file contracts | Owns | Consumes | Consumes | Consumes |
| Query and normalize public CDX | Defines policy | Owns through stock dependency | Calls | — |
| Retrieve archived content | Defines source shapes | Owns | Calls | — |
| Verify downloaded payload digests | Defines strict policy | Owns | Reports failures | — |
| Group captures by exact URL | Defines profile | Owns | Records mapping | — |
| Deduplicate within one URL | Defines profile | Owns | Configures | — |
| Write one WARC 1.0 file per URL | Defines profile | Owns | Manages paths | — |
| Manage user-facing collection job | — | — | Owns | — |
| Create inventory, CDXJ, and manifest | Defines contract | Returns inputs | Owns | Consumes |
| Validate and publish collection | Defines contract | Validates records | Owns | — |
| Serve and rewrite archived pages | — | — | — | Owns |

Ownership is exclusive where possible. Stage 2 does not create Archive Magic manifests or publish collections. Stage 3 does not maintain a second downloader, deduplication engine, or WARC serializer. Stage 4 does not become part of the Fetch job or deployment.

## 8. Future enhancement: WARC 1.1 multi-URL packing

A future collection profile may adopt WARC 1.1, place records for multiple exact URLs in each WARC, and deduplicate identical payloads across those URLs.

That profile would introduce:

- A collection-wide canonical digest map rather than a fresh per-URL map.
- `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` for cross-URL revisits.
- WARC packing and rollover by an approximate target size rather than URL boundaries.
- Canonical references that may cross URL groups and WARC files.
- Additional replay-interoperability and self-containment tests.
- A new versioned collection/profile declaration so readers can distinguish it from the initial WARC 1.0 layout.

The benefit is reduced storage when identical images, stylesheets, scripts, fonts, or other payloads appear under different URLs, together with fewer and larger WARC files. The costs are collection-wide state, more complex references, rollover coordination, and harder partial recovery.

This enhancement is deliberately deferred. The initial one-URL-per-WARC profile does not retain cross-URL digest state or attempt to optimize file counts.

## 9. Accepted tradeoffs and safeguards

### Many small files

One WARC and CDXJ per exact URL may create large file and manifest counts. This is an accepted KISS tradeoff for the initial profile. Tests and early deployments should measure directory, checksum, copy, and Replay startup costs; observed operational limits can justify the future multi-URL profile.

### Strict source-digest verification

Treating a valid digest mismatch as fatal may reject an archive whose playback representation legitimately differs from the indexed payload. Each supported adapter must prove its representation before release. Source-specific transformation policy is added only for a concrete verified source behavior.

### Stock dependency behavior

Archive Magic assumes no ownership of upstream `cdx_toolkit`. Fetch pins an exact tested release and exercises its CDX behavior with deterministic fixtures. Upgrades are intentional and must pass the Fetch core regression suite.

### Public archive changes

CDX, playback, and public-WARC services can change formats, throttling, or error behavior. Fetch uses source-specific fixtures, conservative pacing, bounded retries, visible failures, and explicit non-blocking live tests.

### Large single-URL histories

A single exact URL with many or very large captures produces one correspondingly large WARC in the initial profile. Fetch streams payloads where practical and documents resource requirements. Splitting one URL across files is not added until a concrete limit justifies the additional reference and naming policy.

## 10. Final decision record

Archive Magic will proceed with these decisions:

1. Keep four separately accepted stages: foundation, Fetch core, Fetch application, and Replay.
2. Use stock, exactly pinned `cdx_toolkit` for CDX discovery without maintaining a fork.
3. Put payload retrieval, strict digest verification, response/revisit policy, and WARC writing in the Fetch core.
4. Use WARC 1.0 initially.
5. Group captures by exact target URL and create one WARC file per URL.
6. Create and discard a digest map for each URL; retain no cross-URL canonical state.
7. Trust repeated source digests for fetch skipping only after the first payload for that URL and digest has been downloaded and verified.
8. Treat a valid source digest mismatch as fatal rather than maintaining a second actual-digest deduplication system.
9. Generate one CDXJ per closed per-URL WARC and never mutate indexed bytes.
10. Publish portable, checksummed, immutable collections through one atomic directory rename.
11. Keep Replay separate through the completed collection file contract.
12. Defer WARC 1.1, multi-URL WARC packing, and cross-URL payload deduplication as one coherent future profile.

This architecture intentionally favors simple per-URL state and transparent files over minimizing file count or maximizing deduplication. It leaves a clear upgrade path when cross-URL storage savings justify the additional WARC 1.1 machinery.
