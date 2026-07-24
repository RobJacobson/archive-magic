# Archive Magic Fetch Architecture

**Status:** Implemented MVP architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 23, 2026

## 1. Decision

`archive-magic-fetch` is a small Python CLI that exports Internet Archive
Wayback Machine captures into WARC 1.0 files.

The MVP does three things:

1. Query the Wayback CDX index for a URL pattern and time range.
2. Omit redirects and retrieve each remaining capture needed to establish a
   canonical semantic payload.
3. Write one gzip-compressed WARC for each CDX URL-key resource family.

A resource family is the set of captures represented by one CDX
`urlkey`/SURT. It can include HTTP, HTTPS, `www`, and other URL variants that
Wayback's CDX canonicalization groups together. Fetch writes one WARC and uses
one deduplication namespace for the family; it does not create a separate file
for every original URL spelling.

The implementation follows KISS and YAGNI:

- One Python process.
- Serial discovery and retrieval.
- One archive source: the Internet Archive Wayback Machine.
- One shared `WaybackSession` and `WaybackClient` per command.
- Public `wayback` client APIs for CDX discovery and Memento playback.
- Upstream `CdxRecord` and `Memento` values without a local archive model.
- Semantic response bodies rather than raw source-WARC representation bytes.
- `warcio` for WARC construction and serialization.
- Standard-library `argparse` and small console messages.
- No application/core hierarchy, source-adapter framework, plugin system,
  persisted inventory, or publication transaction.

All Fetch implementation code remains in the flat
`src/archive_magic_fetch/` package. Common Crawl and other archives are
outside the current product contract and do not justify a generic archive
client abstraction.

The Git repository remains rooted at the parent `archive-magic/` directory,
but Fetch is a self-contained project beneath `archive-magic-fetch/`. Its
source, tests, dependency metadata, lockfile, license, development settings,
and documentation do not depend on another project in the repository.
Generated archive data is the one substantive sibling and lives in
`archive-magic/archives/`.

The separate `archive-magic-replay` project is outside this document.

## 2. CLI contract

The command accepts one URL pattern and optional numeric CDX dates:

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

Examples:

```bash
archive-magic-fetch 'https://example.com/'
archive-magic-fetch 'https://example.com/*' --start 2018 --end 2020
archive-magic-fetch 'https://example.com/images/*' --start 20200101
```

Numeric partial values such as `2020`, `20200131`, and `20200131153000` are
passed through to `WaybackClient.search()`.

Defaults remain explicit:

```python
date_start = args.start or "1995"
date_end = args.end or current_utc_cdx_timestamp()
```

Consequently:

- Neither bound means 1995 through the present.
- Start only means that date through the present.
- End only means 1995 through that date.

Fetch always supplies both bounds. It does not impose a result limit,
collapse, status filter, or MIME filter. It explicitly disables server-side
revisit resolution because Fetch performs its own payload reuse policy.

Discovery remains complete so the command can report how many CDX rows were
selected and how many redirects were omitted. Known 3xx rows are removed
locally before playback, so completeness does not cost a Memento request for
each redirect.

The MVP has no output argument. Commands are run from the
`archive-magic-fetch/` project directory, and output is written beneath the
sibling `../archives/` directory:

```bash
cd archive-magic-fetch
uv run archive-magic-fetch 'https://example.com/'
```

An empty result is successful:

```text
No captures found
```

Invalid arguments and fatal job errors produce a nonzero exit status.

## 3. Wayback dependency and client lifecycle

Fetch pins `wayback==0.5.1`. The package is maintained by the Environmental
Data & Governance Initiative and is purpose-built for the Internet Archive
Wayback Machine; it is not maintained by Internet Archive itself.

The CLI creates one descriptive session and gives ownership of it to one
client context:

```python
session = WaybackSession(
    user_agent=(
        "archive-magic-fetch/0.1.0 "
        "(+https://github.com/RobJacobson/archive-magic)"
    )
)
with WaybackClient(session=session) as client:
    captures = discover(client, ...)
    ...
    export_all(..., client)
```

The client spans discovery, output preflight, and export. Exiting the client
context closes the supplied session and its pooled network connections,
including on an empty result or fatal error.

Fetch uses the `WaybackSession` 0.5.1 endpoint-specific default pacing:

```text
CDX search:       0.4 calls/second (one call every 2.5 seconds)
Memento playback: 8 calls/second   (one call every 0.125 seconds)
```

These are application-independent library limits selected in collaboration
with Internet Archive staff. Fetch does not add a fixed delay around every
capture, override these defaults, or reproduce the library's retry/backoff
transport.

The application adds one bounded `RateLimitError` rule around each discovery
or Memento operation:

1. On the first rate limit, sleep for `retry_after`, falling back to 60
   seconds.
2. Retry that complete operation once.
3. If the retry is also rate-limited, propagate the error and stop the job.

For discovery, a complete operation means fully materializing the lazy search
iterator. Partial results from a rate-limited attempt are discarded before
the search is restarted. For retrieval, the same selected `CdxRecord` is
requested again.

Execution remains serial. Concurrency is not required to remove the former
six-second host-wide delay and would need shared rate-limit coordination if
added later.

## 4. Output layout and preflight

Each CDX URL-key group maps to one stable WARC path beneath the repository's
root-level `archives/` directory:

```text
archive-magic/
├── archive-magic-fetch/
└── archives/
    └── urlkey/
        └── com%2Cexample%29/
            ├── index--a1b2c3d4e5f6.warc.gz
            ├── images/
            │   └── logo.png--e4f5a6b7c8d9.warc.gz
            └── css/
                └── site.css--91c2d3e4f5a6.warc.gz
```

The mapping rules are:

1. The first directory is the literal `urlkey` namespace.
2. URL-key path segments become nested directories.
3. The URL-key authority/SURT component becomes a safely encoded directory.
4. The final path segment becomes the readable filename stem.
5. A root URL or path ending in `/` uses `index` as the stem.
6. The stem ends with the first 12 lowercase hexadecimal characters of
   SHA-256 over the complete `urlkey`, followed by `.warc.gz`.
7. Query strings are not written literally; URL-key differences remain
   distinguished by the hash.
8. Empty, `.`, `..`, separators, control characters, and platform-unsafe
   characters cannot escape or reshape the output root.

From the project directory, the default output root is `../archives`. Fetch
computes and checks every selected output path before Memento retrieval. It
fails if two URL-key groups map to the same path or if a target already
exists.

WARC creation itself uses exclusive mode. The MVP does not overwrite, append,
merge, or resume.

## 5. Capture model and discovery

Discovery uses the public high-level search API:

```python
captures = list(
    client.search(
        url_pattern,
        from_date=date_start,
        to_date=date_end,
        resolve_revisits=False,
    )
)
```

`WaybackClient.search()`:

- interprets the supported exact, prefix, and domain URL patterns;
- paginates with resume keys;
- includes recent results omitted by older numbered-page approaches;
- applies its default malformed-result filtering;
- parses timestamps into timezone-aware UTC `datetime` values;
- removes matching explicit default ports such as HTTP `:80` and HTTPS
  `:443`; and
- returns immutable, value-equal, hashable `CdxRecord` values.

Fetch accepts those upstream URL and timestamp semantics. It does not maintain
a second URL normalizer for fragments, bare queries, or default ports, and it
does not preserve malformed timestamp syntax as an independent identity.

The public capture fields used by Fetch are:

```text
urlkey
original
timestamp
statuscode
digest
```

`mimetype` and `length` remain part of `CdxRecord` equality even though export
does not otherwise use them.

After complete materialization, Fetch:

1. Collapses only value-equal duplicate `CdxRecord` values.
2. Groups records by `urlkey` in first-seen group order.
3. Sorts each group by the parsed `timestamp`.

All results remain in memory. Fetch does not persist a source inventory, CDX
file, CDXJ, manifest, database, or checkpoint.

Incomplete discovery is fatal because Fetch cannot know the complete set of
captures or safely preflight all output paths.

## 6. Data flow

```text
CLI arguments
    -> apply explicit date defaults
    -> create one WaybackSession and WaybackClient
    -> fully materialize WaybackClient.search()
         -> on RateLimitError, wait and retry the whole search once
    -> collapse value-equal CdxRecord duplicates
    -> group by CdxRecord.urlkey
    -> sort each group by CdxRecord.timestamp
    -> preflight every output path
    -> export each URL-key group independently
         -> count and omit known CDX 3xx rows before playback
         -> initialize source and semantic digest maps
         -> use a successful source signature when already known
         -> otherwise retrieve the exact Memento
              -> on RateLimitError, wait and retry once
         -> convert Memento semantic content into a WARC response
         -> reject a known CDX/Memento status mismatch
         -> count and omit a 3xx discovered from a statusless CDX row
         -> write a response or identical-payload-digest revisit
         -> publish deduplication state only after the write succeeds
         -> close the WARC
    -> print one aggregate export summary
    -> close the Wayback client/session
```

Grouping by `urlkey` lets straightforward variants share one WARC and one
deduplication namespace. Responses and revisits retain the appropriate
capture-specific target identity described below. A revisit may refer to a
canonical response whose target URI is another URL variant in the same group.

## 7. Memento retrieval and response construction

### 7.1 Exact playback

For an unseen source signature, Fetch delegates playback routing and response
interpretation to the public client:

```python
with client.get_memento(
    capture,
    mode=Mode.original,
    exact=True,
    follow_redirects=False,
) as memento:
    payload = memento.content
    ...
```

Known CDX 3xx rows never reach this call. For remaining rows, the options mean:

- `Mode.original` avoids toolbar injection and browsing-oriented URL
  rewriting.
- `exact=True` rejects ordinary substitution with a nearby capture.
- `follow_redirects=False` prevents traversal when playback itself returns a
  historical redirect.

Wayback's URL canonicalization can still return a different URL variant or
status at the same timestamp. Fetch therefore treats a known CDX status as a
required playback invariant. A mismatch is warned, omitted, and never allowed
to seed deduplication.

The Memento context guarantees that the underlying HTTP response closes on
success and on WARC-construction failure.

Fetch does not construct playback URLs, issue direct Requests calls, parse
Wayback routing headers, or maintain a parallel playback exception hierarchy.

### 7.2 Semantic payload policy

`Memento.content` is the semantic response body that Fetch writes. It may not
match the representation bytes Internet Archive stored in a source WARC
because HTTP content coding and playback transfer coding can be decoded during
delivery.

This is intentional. Fetch wants equivalent compressed and uncompressed
deliveries of the same content to deduplicate to the same semantic payload.

Consequently, Fetch does not:

- stream raw playback bytes;
- disable Requests decoding;
- compare raw and decoded candidates;
- decode gzip or deflate itself;
- infer whether a content encoding came from the archive or playback;
- compare Memento content with the CDX digest; or
- reject a Memento because its semantic digest differs from the CDX digest.

The CDX digest remains useful as a post-success retrieval-skipping hint, not as
a fixity assertion over `Memento.content`.

### 7.3 WARC response fields

The response is built from public Memento fields:

```text
WARC-Target-URI      memento.url
WARC-Date            memento.timestamp normalized to UTC second precision
WARC-Source-URI      memento.memento_url
HTTP status          memento.status_code
HTTP headers         filtered memento.headers
HTTP body            memento.content
WARC-Payload-Digest  SHA-1 over memento.content, produced by warcio
```

Fetch uses the standard-library HTTP reason phrase for a known status code. An
unknown numeric status is preserved without inventing a reason phrase.

Archived 4xx and 5xx statuses are valid content responses and are preserved.
Archived 3xx responses are deliberately omitted. When CDX supplies a numeric
status, the Memento status must match it; a mismatch is treated as playback
substitution rather than exported under the wrong CDX identity.

### 7.4 Header normalization

`Memento.headers` contains the historical headers reconstructed by the
`wayback` client. Fetch removes fields that would incorrectly describe or
validate a different stored representation:

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

Matching is case-insensitive. Fetch then adds one `Content-Length` equal to the
semantic payload length. Other reconstructed historical headers, including
`Content-Type` and cache metadata, are retained.

## 8. Per-group export and deduplication

Each CDX URL key is an independent processing unit. Its WARC contains all
successfully preserved captures for the URL variants in that group, in
chronological order.

Three maps are scoped to one `export_group()` call:

```text
(CDX digest, known CDX status)
    -> semantic payload digest and canonical response

CDX digest
    -> first successful semantic payload digest and canonical response
       used only when a later CDX row has no status

(semantic payload digest, actual Memento status)
    -> canonical response
```

Statuses remain integers or `None`; they are not stringified for map keys.

### 8.1 Source-signature reuse

A usable CDX digest is normalized to `sha1:` plus uppercase Base32.

Known CDX 3xx captures are omitted before consulting any map. For another
capture with known CDX status, Fetch may skip playback only after the same
`(CDX digest, CDX status)` has already produced a successfully written response
or revisit in the current group.

A CDX revisit or unknown-status row may use the first successful canonical
response known for its digest. If no successful occurrence exists yet, Fetch
retrieves it normally and uses the Memento's actual status.

A missing or malformed CDX digest always causes retrieval. It cannot seed or
match a source map, but the retrieved payload still participates in semantic
deduplication.

Source-map entries are published only after:

1. Exact Memento retrieval succeeds.
2. A usable semantic WARC response identity exists.
3. Any known CDX status matches the actual Memento status.
4. The actual status is not 3xx.
5. The response or revisit write succeeds.

A failed first occurrence therefore does not prevent a later occurrence from
being retrieved.

When playback is skipped, no Memento exists. The revisit uses
`CdxRecord.original` and `CdxRecord.timestamp` for its current target and date,
while its reference fields name the canonical Memento-derived response.

### 8.2 Semantic deduplication

Every retrieved response has a semantic identity:

```text
(WARC-Payload-Digest over Memento.content, actual HTTP status)
```

If that identity has not appeared, Fetch writes the full response and records
it as canonical.

If it has appeared, Fetch writes an identical-payload-digest revisit using the
retrieved Memento's target URL and timestamp. The later capture's distinct CDX
source signature, if usable, maps to the original full canonical response, not
to the newly written revisit.

Status remains part of semantic identity so identical bodies served as, for
example, 200 and 404 are not conflated.

Different CDX digests can converge on the same semantic payload and status.
They are each retrieved once to establish that convergence; later occurrences
of either successful source signature can skip playback.

### 8.3 Core policy

The implemented policy can be summarized as:

```python
for capture in captures:
    if is_3xx(capture.statuscode):
        count_omitted_redirect(capture)
        continue

    source_match = find_successful_source_match(
        capture.digest,
        capture.statuscode,
    )
    if source_match is not None:
        write_revisit(
            target=capture.original,
            date=capture.timestamp,
            canonical=source_match.canonical,
        )
        continue

    try:
        response = retrieve_exact_semantic_response(client, capture)
    except SKIPPABLE_WAYBACK_ERRORS as error:
        warn_and_continue(capture, error)
        continue

    if (
        capture.statuscode is not None
        and response.actual_status != capture.statuscode
    ):
        warn_status_substitution(capture, response.actual_status)
        continue

    if is_3xx(response.actual_status):
        count_omitted_redirect(capture)
        continue

    semantic_key = (
        response.payload_digest,
        response.actual_status,
    )

    if writer is None:
        writer = open_new_warc_exclusively(path)

    canonical = semantic_canonicals.get(semantic_key)
    if canonical is None:
        canonical = write_response(writer, response)
    else:
        write_revisit(
            target=response.memento_target,
            date=response.memento_date,
            canonical=canonical,
        )

    remember_semantic_key_after_success(semantic_key, canonical)
    remember_source_key_after_success(capture, canonical)
```

This pseudocode describes policy rather than required internal names.

### 8.4 Redirects

Fetch is a content-oriented export, not a lossless inventory of every CDX
capture event. HTTP 3xx records usually describe a crawler reaching a
noncanonical scheme, host spelling, `www` alias, or former URL rather than a
new historical document. Preserving them would add playback requests, WARC
noise, and unavailable-capture policy without adding content.

The implemented policy is deliberately uniform:

- A row with a known CDX status from 300 through 399 is counted and omitted
  before playback.
- A statusless CDX row is retrieved normally; if its Memento status is 3xx, it
  is counted and omitted.
- Fetch does not classify redirects as canonicalization versus substantive
  relocation, follow them, synthesize them, or create metadata placeholders.
- Redirect omissions do not warn individually. The final command summary
  reports their aggregate count.

This uniform rule follows KISS. A future opt-in redirect-preservation feature
may be added if a concrete replay or URL-migration use case justifies the
additional requests and policy.

## 9. WARC profile

Every output file:

- Uses WARC 1.0.
- Uses record-at-a-time gzip compression through
  `warcio.WARCWriter(gzip=True)`.
- Begins with a minimal `warcinfo` record identifying Archive Magic and WARC
  1.0.
- Contains response and revisit records for one CDX URL-key group.
- Uses Memento target/date/source identity for fully retrieved responses.
- Uses CDX target/date identity only for current captures whose successful
  source signature avoids another retrieval.
- Includes semantic payload digests over the bodies actually written.
- Writes canonical responses before dependent revisits.
- Includes `WARC-Refers-To`, `WARC-Refers-To-Target-URI`, and
  `WARC-Refers-To-Date` on revisits.
- Writes an empty revisit body rather than a duplicate payload.
- Contains no fabricated request, redirect, or unavailable-capture metadata
  records.

`WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` are standardized by WARC
1.1. The MVP uses them as extension fields in WARC 1.0 so a cross-target
revisit can name its canonical response precisely.

Capture datetimes must be timezone-aware. They are normalized to UTC, truncated
to second precision, and written with a `Z` suffix:

```text
2020-01-02T03:04:05Z
```

If every capture for a URL-key group is skipped, Fetch creates no file for
that group. It does not leave a `warcinfo`-only WARC or unavailable placeholder
record.

## 10. Failure and logging policy

### 10.1 Skippable capture failures

The following public Wayback retrieval errors warn, skip only the selected
capture, and allow later captures and groups to continue:

- `MementoPlaybackError`, including `NoMementoError`;
- `BlockedByRobotsError`;
- `BlockedSiteError`; and
- exhausted `WaybackRetryError`.

A skipped capture never seeds source or semantic deduplication state.

A known CDX/Memento status mismatch is also warned and skipped. It indicates
that Wayback returned a different capture identity despite exact playback
routing. Redirect omissions are intentional content filtering, not playback
failures, and do not produce per-capture warnings.

### 10.2 Fatal job failures

Fetch stops for:

- invalid CLI input;
- incomplete or malformed CDX discovery;
- `UnexpectedResponseFormat`;
- a second `RateLimitError` for the same operation;
- malformed local response/WARC state;
- programming errors;
- two selected groups mapping to one output path;
- an existing target WARC; and
- directory creation, file writing, compression, or WARC serialization
  failures.

Fetch intentionally does not catch every exception as a remote skip. Doing so
would conceal local corruption and programming defects.

It does not fabricate a broken-resource response. Absence of a record naturally
produces a missing resource during replay.

### 10.3 Console output

Console output remains deliberately small:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: capture unavailable
Summary: 235 selected; 209 responses; 17 revisits; 9 redirects omitted; 0 playback failures
```

Messages are:

- `Starting ...` once for each URL-key group, with a URL-variant count when
  needed.
- `Downloaded ... [hash]` after each successful Memento retrieval and
  response/revisit write, using the last eight characters of the semantic
  payload digest.
- `WARNING skipped ...` for each approved skippable retrieval error.
- `WARNING skipped ...` when the known CDX status and playback status differ.
- `Summary: ...` once after all groups, including response, revisit, omitted
  redirect, and playback-failure counts.
- `ERROR: ...` at the CLI boundary for a fatal job error.

A source-signature revisit that avoids the network is written silently.

No logging framework, progress bar, log file, structured event protocol, or
verbosity configuration is required.

## 11. Project layout and responsibilities

```text
archive-magic/
├── .git/
├── .gitignore
├── archive-magic-fetch/
│   ├── .gitignore
│   ├── .python-version
│   ├── LICENSE
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── docs/
│   │   ├── ARCHITECTURE-FETCH.md
│   │   ├── WARC_CDX_DEDUPLICATION_RESEARCH_MEMO.md
│   │   └── WAYBACK-CLIENT-REWRITE-MEMO.md
│   ├── src/
│   │   └── archive_magic_fetch/
│   │       ├── __init__.py
│   │       ├── cli.py
│   │       ├── discovery.py
│   │       ├── paths.py
│   │       ├── export.py
│   │       ├── retrieval.py
│   │       └── warc.py
│   └── tests/
│       ├── test_discovery.py
│       ├── test_paths.py
│       ├── test_export.py
│       └── test_retrieval.py
└── archives/
    └── urlkey/
```

The parent directory supplies only Git repository control and generated
archive storage. The Fetch project can resolve dependencies, run tests, build,
and execute from `archive-magic-fetch/` without importing files from the
parent or another sibling.

The modules have narrow responsibilities:

| File | Responsibility | Principal functions |
| --- | --- | --- |
| `cli.py` | Arguments, date defaults, shared Wayback client lifetime, command error boundary | `parse_args()`, `main()` |
| `discovery.py` | Lazy CDX materialization, discovery rate-limit retry, value deduplication, URL-key grouping | `discover()`, `group_captures()` |
| `paths.py` | Safe deterministic URL-key paths and complete preflight checks | `urlkey_warc_path()`, `preflight_paths()` |
| `retrieval.py` | Exact Memento retrieval, one rate-limit retry, semantic header filtering, WARC response construction | `retrieve_response()` |
| `export.py` | Redirect filtering, status validation, per-group source/semantic maps, response-versus-revisit decisions, aggregate outcomes, console messages | `ExportSummary`, `export_all()`, `export_group()` |
| `warc.py` | WARC date normalization, exclusive file creation, response and revisit serialization | `timestamp_to_warc_date()`, `open_new_warc()`, `write_response()`, `write_revisit()` |

No `core/`, `application/`, `adapters/`, `models/`, `interfaces/`, or
`services/` hierarchy is required. Add a new file or abstraction only when a
concrete implementation responsibility can no longer remain clear as a small
function in the existing package.

The package supports Python 3.12 or newer. Local development selects Python
3.14 through `.python-version`. The exactly pinned direct runtime dependencies
are:

```text
wayback==0.5.1
warcio==1.8.1
```

Requests remains a transitive implementation dependency of `wayback`; Fetch
does not import or call it directly.

`pytest>=8` belongs to the default `dev` dependency group. `uv` manages the
local interpreter, `.venv`, lockfile, and development dependencies; it is a
development workflow rather than part of Fetch's runtime architecture.

## 12. Testing and acceptance

Routine tests use real `CdxRecord` values, fake clients/Mementos, and temporary
directories. They do not contact Internet Archive.

Routine local development is activation-free and runs from the standalone
project directory:

```bash
cd archive-magic-fetch
uv run pytest
uv run archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

A clean acceptance check also runs:

```bash
uv lock --check
uv run archive-magic-fetch --help
```

Generated environments, caches, and build products are excluded by the
project `.gitignore`. The repository-root `.gitignore` excludes the sibling
`archives/` output.

The implemented MVP is accepted when deterministic tests demonstrate:

1. Missing date bounds expand to 1995 and/or the present.
2. One session/client spans discovery and export and closes at the command
   boundary.
3. Discovery passes both bounds, disables revisit resolution, and fully
   materializes the lazy search.
4. A rate limit after partial discovery discards the partial attempt and
   retries complete materialization once.
5. A missing `retry_after` waits 60 seconds; a second rate limit is fatal.
6. Captures collapse only by `CdxRecord` value equality, group by `urlkey` in
   first-seen order, and sort by aware datetime.
7. Upstream default-port normalization is accepted.
8. URL-key paths map safely and deterministically beneath `../archives/`.
9. Existing targets and path collisions fail before Memento retrieval.
10. Retrieval passes `Mode.original`, `exact=True`, and
    `follow_redirects=False`.
11. Mementos close on success and on WARC-construction failure.
12. Response target/date/source/status/headers/body come from the Memento.
13. Representation-dependent headers are removed and semantic
    `Content-Length` is correct.
14. The WARC payload digest matches the semantic body written.
15. Known and unknown HTTP statuses produce valid status lines.
16. Known CDX 3xx rows are counted and omitted without playback.
17. A statusless row whose retrieved Memento is 3xx is counted and omitted.
18. A known CDX/Memento status mismatch warns, is omitted, and does not seed
    deduplication.
19. CDX digest/status reuse begins only after successful retrieval,
    status validation, and write.
20. A failed first source occurrence does not suppress a later retrieval.
21. Known CDX statuses and actual response statuses remain distinct integer
    values until playback validation succeeds.
22. Statusless CDX revisits use a previously successful digest occurrence or
    retrieve normally when none exists.
23. Different CDX digests that converge on one semantic payload/status produce
    one response and revisits.
24. Missing and malformed CDX digests retrieve normally and still participate
    in semantic deduplication.
25. Source-signature shortcuts use CDX current identity while fetched
    responses and semantic revisits use Memento identity.
26. Approved Wayback availability failures warn and continue.
27. Unexpected formats, repeated rate limits, filesystem errors, and WARC
    serialization errors remain fatal.
28. An all-skipped group produces no output file.
29. Deduplication maps do not cross URL-key group boundaries.
30. The final summary accounts for selected rows, responses, revisits,
    intentionally omitted redirects, and playback failures.
31. `warcio` parses each output as gzip-compressed WARC 1.0 with the expected
    response/revisit order, semantic digests, and canonical references.

A small manually invoked Internet Archive smoke export may verify current
upstream behavior. Remote availability is not part of the deterministic suite.

## 13. Explicit non-goals

The MVP does not include:

- Replay or any `archive-magic-replay` code.
- Common Crawl or other archives.
- A generic archive client interface.
- HTML dependency discovery or page bundles.
- Exact preservation of source-WARC representation or transfer bytes.
- CDX-digest fixity validation against Memento content.
- Cross-URL-key deduplication.
- Redirect preservation, redirect classification, or unavailable-capture
  metadata.
- WARC 1.1.
- CDXJ generation or replay indexes.
- Persisted CDX inventories, manifests, checksums, schemas, or databases.
- Atomic publication, temporary staging, resumability, or cleanup tooling.
- Overwrite, merge, append, or repair behavior.
- Concurrent downloads, queues, services, or workers.
- Configurable request rates or retry counts.
- Configuration files, environment-variable configuration, or plugin systems.
- Machine-readable progress events or a graphical interface.

## Appendix A: Potential future enhancements

Consider these only after the MVP has been exercised:

- Add a separate Common Crawl fetch client using public source-WARC byte
  ranges.
- Add configurable output roots and deliberate overwrite or resume behavior.
- Add page bundles by discovering static HTML and CSS dependencies.
- Add WARC 1.1 if standardized cross-target references or broader replay
  interoperability requires it.
- Add CDXJ indexes if a concrete replay workflow requires them.
- Add a persisted inventory or manifest when auditability or restartability is
  required.
- Add bounded concurrency only after measuring serial throughput and
  coordinating all workers through shared rate limits.
- Add richer progress, summaries, and machine-readable output.
- Preserve authentic request records if a source exposes them.
- Add atomic temporary-file replacement and stale-partial cleanup.

## Appendix B: Dependency and standards references

- [`wayback` 0.5.1 usage and API documentation](https://wayback.readthedocs.io/en/stable/usage.html)
- [`wayback` 0.5.1 client source and pacing behavior](https://wayback.readthedocs.io/en/stable/_modules/wayback/_client.html)
- [`CdxRecord` digest and `Memento` model source](https://wayback.readthedocs.io/en/stable/_modules/wayback/_models.html)
- [`wayback` exception hierarchy](https://wayback.readthedocs.io/en/stable/_modules/wayback/exceptions.html)
- [`wayback` 0.5.1 release history](https://wayback.readthedocs.io/en/stable/release-history.html)
- [Internet Archive automated-access guidance](https://archive.org/developers/bots.html)
- [IIPC WARC 1.0 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.0/)
- [`warcio` documentation](https://warcio.readthedocs.io/en/latest/)
