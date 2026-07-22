# Archive Magic Fetch Architecture

**Status:** MVP architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 22, 2026

## 1. Decision

`archive-magic-fetch` is a small Python CLI that exports Internet Archive captures into WARC 1.0 files.

The MVP does only three things:

1. Query the Internet Archive CDX index for a URL pattern and time range.
2. Fetch each distinct payload needed by the selected captures.
3. Write one gzip-compressed WARC file for each exact resource URL.

An exact resource URL may identify HTML, CSS, JavaScript, an image, or any other captured resource. Fetch does not attempt to discover which assets belong to an HTML page or bundle page dependencies together.

The implementation follows KISS and YAGNI:

- One Python process.
- One archive source: the Internet Archive.
- One WARC profile: WARC 1.0.
- One in-memory digest map per exact URL.
- Stock `cdx_toolkit`; no fork.
- `warcio` for WARC serialization.
- Standard-library `argparse` and simple console output.
- No application/core layers, plugin system, source adapters, manifests, or publication transaction.

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

Dates use the numeric format already accepted by `cdx_toolkit`, including partial values such as `2020`, `20200131`, or `20200131153000`.

Defaults are deliberately explicit so upstream protective defaults cannot shorten the request:

```python
date_start = args.start or "1995"
date_end = args.end or current_utc_cdx_timestamp()
```

Consequently:

- Neither bound means 1995 through the present.
- Start only means that date through the present.
- End only means 1995 through that date.

Fetch supplies both bounds to `cdx_toolkit.CDXFetcher(source="ia").iter(...)` and does not impose a result limit, collapse, status filter, or MIME filter.

The MVP has no output argument. Output is written beneath `./warcs/` relative to the working directory.

## 3. Output layout

Each exact resource URL maps to one WARC path that mirrors its URL hierarchy:

```text
warcs/
└── https/
    └── example.com/
        ├── index--a1b2c3d4e5f6.warc.gz
        ├── images/
        │   └── logo.png--e4f5a6b7c8d9.warc.gz
        └── css/
            └── site.css--91c2d3e4f5a6.warc.gz
```

The mapping rules are:

1. The first directory is the URL scheme.
2. The second directory is the host, including a port when present.
3. URL path segments become nested directories.
4. The final path segment becomes the readable filename stem.
5. A root URL or path ending in `/` uses `index` as the stem.
6. The stem ends with the first 12 lowercase hexadecimal characters of SHA-256 over the exact URL, followed by `.warc.gz`.
7. Query strings are not written literally; they are distinguished by the URL hash.
8. Every filesystem component is encoded into a safe single segment. Empty, `.`, `..`, separators, control characters, and platform-unsafe characters cannot escape or reshape the output root.

The URL hash prevents common collisions while keeping paths recognizable. Fetch also checks the complete set of generated paths before downloading payloads and fails if two selected URLs map to the same path.

An existing target WARC is an error. The MVP does not overwrite, append, merge, or resume.

## 4. Data flow

```text
CLI arguments
    -> apply explicit date defaults
    -> query Internet Archive through cdx_toolkit
    -> materialize CDX captures in memory
    -> group by exact capture URL
    -> sort each group by capture timestamp
    -> preflight output paths
    -> export each exact URL independently
         -> create a fresh digest map
         -> fetch unseen non-redirect payloads
         -> write responses or revisits
         -> close the WARC
```

CDX results exist only in memory. Fetch does not persist a source inventory, CDX file, CDXJ, manifest, database, or checkpoint.

Grouping uses the exact `url` returned for each capture, not the CDX `urlkey` or SURT. Distinct HTTP and HTTPS URLs, query strings, trailing slashes, and other exact-URL differences remain distinct resources and produce separate WARCs.

## 5. Discovery

Discovery uses the paged iterator from stock `cdx_toolkit`:

```python
fetcher = cdx_toolkit.CDXFetcher(source="ia")
captures = list(
    fetcher.iter(
        url_pattern,
        from_ts=date_start,
        to=date_end,
    )
)
```

The precise call may change with a pinned dependency version, but these behaviors must remain:

- Internet Archive is the only source.
- Both time bounds are always explicit.
- Iteration is paged and uncollapsed.
- Every returned capture is retained for processing.
- Captures are grouped by exact URL and ordered by timestamp.

An empty result is a successful command that reports that no captures were found and writes no files. A failure to complete CDX discovery stops the command because Fetch cannot know what should be exported.

## 6. Per-URL export

Each exact URL is an independent processing unit. Its WARC contains all successfully preserved captures for that URL in chronological order.

The digest map is local to that URL:

```text
normalized payload digest -> canonical response record ID and capture date
```

It is discarded when the WARC closes. Identical bytes at different URLs are downloaded and stored independently.

The core loop is:

```python
for url, captures in captures_by_url:
    seen = {}
    writer = None

    log_start(url)

    for capture in captures:
        expected = normalize_digest(capture.get("digest"))

        if not is_redirect(capture) and expected in seen:
            write_revisit(writer, capture, seen[expected])
            continue

        try:
            response = capture.fetch_warc_record()
            use_cdx_identity(response, capture)
            actual = calculated_payload_digest(response)
        except CaptureRetrievalError as error:
            warn_and_skip(capture, error)
            continue

        if expected is not None and actual != expected:
            warn_and_skip(capture, "payload digest mismatch")
            continue

        if writer is None:
            writer = open_new_warc(url)

        canonical = write_response(writer, response)
        log_download(capture, actual)

        if not is_redirect(capture):
            seen.setdefault(actual, canonical)

    close_if_open(writer)
```

This pseudocode describes policy rather than required names or exception types.

### 6.1 Retrieval

For every capture whose usable digest has not been seen in the current URL, Fetch calls `CaptureObject.fetch_warc_record()`.

For Internet Archive captures, `cdx_toolkit` requests exact-timestamp Wayback playback and constructs an in-memory response record. Fetch reuses this behavior instead of implementing a second Wayback HTTP client and header-reconstruction layer.

Fetch does not use the stock `cdxt warc` exporter. That exporter fetches duplicate captures and writes them as full responses; it does not implement the required revisit policy.

The selected CDX row remains authoritative for the exact target URL and capture timestamp. Fetch replaces the synthesized response record's `WARC-Target-URI` and `WARC-Date` with those CDX values before writing it.

### 6.2 Digests

Internet Archive CDX digests are normalized into the form used by `warcio`, normally `sha1:` followed by uppercase Base32.

For a downloaded response, the calculated digest is the `WARC-Payload-Digest` produced from the response payload by the `cdx_toolkit`/`warcio` record construction path.

- A matching usable CDX digest verifies the response.
- A missing or malformed CDX digest forces a download and permits the calculated digest to become canonical.
- A mismatch logs a warning and skips that capture.
- A skipped or mismatched capture never adds an entry to `seen`.

### 6.3 Revisits

A later non-redirect capture with a digest already verified in the current URL becomes a WARC 1.0 `revisit` record without another playback request.

The revisit uses the identical-payload-digest profile and includes:

- The current capture's exact target URL.
- The current capture timestamp.
- The payload digest.
- `WARC-Refers-To` naming the earlier canonical response record.
- No duplicate payload body.

The canonical response is always written before its revisits. Because each WARC represents one exact URL, revisit references never cross files or URLs.

Not fetching a duplicate means Fetch cannot observe HTTP header changes that are absent from CDX. This loss of per-capture header fidelity is an accepted MVP tradeoff.

### 6.4 Redirects

All 3xx captures are fetched and written as full response records, even when their payload digest has appeared before.

Redirect bodies are commonly empty, so unrelated redirect destinations can share a digest. A digest-only revisit could therefore reuse the wrong `Location` header. Always fetching redirects is the smallest correct rule and redirects do not populate the digest map.

## 7. WARC profile

Every output file:

- Uses WARC 1.0.
- Uses record-at-a-time gzip compression through `warcio.WARCWriter(gzip=True)`.
- Begins with a minimal `warcinfo` record identifying Archive Magic and WARC 1.0.
- Contains response and revisit records for exactly one target URL.
- Preserves the CDX target URL and capture timestamp.
- Includes payload digests.
- Writes canonical responses before dependent revisits.
- Contains no fabricated request records.

If every capture for an exact URL is skipped, Fetch creates no WARC for that URL. It does not create a `warcinfo`-only file or an unavailable placeholder record.

## 8. Failure and logging policy

Remote capture failures degrade gracefully:

- An unavailable capture logs a warning and is skipped.
- An exhausted retrieval failure logs a warning and is skipped.
- A payload-digest mismatch logs a warning and is skipped.
- Other captures and exact URLs continue processing.

Fetch stops for errors that prevent it from safely continuing the local job:

- Invalid CLI input.
- Incomplete CDX discovery.
- Two selected URLs mapping to the same output path.
- An existing target WARC.
- Directory creation, file writing, compression, or WARC serialization failure.

Fetch does not create a synthetic broken-resource response. Absence of a record naturally produces a missing resource during replay.

Console output stays intentionally small:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: capture unavailable
```

Required messages are:

- `Starting ...` once for each exact URL.
- `Downloaded ... [hash]` for each successful network download, using the last eight characters of the normalized payload digest.
- `WARNING ...` for each skipped capture.

No logging framework, structured event protocol, progress bar, log file, or verbosity configuration is required.

## 9. Minimal source layout

```text
archive-magic-fetch/
├── pyproject.toml
├── src/
│   └── archive_magic_fetch/
│       ├── __init__.py
│       ├── cli.py
│       ├── discovery.py
│       ├── paths.py
│       ├── export.py
│       └── warc.py
└── tests/
    ├── test_discovery.py
    ├── test_paths.py
    └── test_export.py
```

The files have narrow responsibilities:

| File | Responsibility | Principal functions |
| --- | --- | --- |
| `cli.py` | Arguments, date defaults, console entry point | `parse_args()`, `main()` |
| `discovery.py` | IA CDX query, materialization, exact-URL grouping | `discover()`, `group_captures()` |
| `paths.py` | Safe deterministic URL-to-WARC paths and preflight checks | `warc_path()`, `preflight_paths()` |
| `export.py` | Per-URL loop, digest map, skip policy, simple messages | `export_all()`, `export_url()` |
| `warc.py` | WARC-specific response preparation and record writing | `prepare_response()`, `write_warcinfo()`, `write_revisit()` |

No `core/`, `application/`, `adapters/`, `models/`, `interfaces/`, or `services/` hierarchy is needed. A new file or abstraction should be introduced only when existing code has two concrete responsibilities that cannot remain clear as small functions.

The initial runtime target is Python 3.12 or newer with exact tested dependency versions:

```text
cdx_toolkit==0.9.39
warcio==1.8.1
```

## 10. Testing and acceptance

Tests use fake `CaptureObject`-like objects and temporary directories. Routine tests do not contact the Internet Archive.

The MVP is accepted when tests demonstrate:

1. Missing date bounds expand to 1995 and/or the present.
2. Discovery passes explicit bounds and no hidden result limit.
3. Captures group by exact URL and sort by timestamp.
4. URL paths map safely and deterministically beneath `./warcs/`.
5. Existing targets and path collisions fail before downloads begin.
6. The first occurrence of a digest is fetched and written as a response.
7. A later same-URL occurrence becomes a revisit without fetching.
8. The same digest at another URL is fetched independently.
9. Redirects are always fetched as full responses.
10. Retrieval failures and digest mismatches warn, skip, and continue.
11. An all-skipped URL produces no WARC.
12. `warcio` can parse each output as WARC 1.0 with the expected response/revisit order, target URL, timestamps, and canonical references.

One small, manually invoked Internet Archive smoke test may verify current upstream behavior. Remote availability is not part of the deterministic test suite.

## 11. Explicit non-goals

The MVP does not include:

- Replay or any `archive-magic-replay` code.
- Common Crawl or other archives.
- HTML dependency discovery or page bundles.
- Cross-URL deduplication.
- WARC 1.1.
- CDXJ generation or replay indexes.
- Persisted CDX inventories, manifests, checksums, schemas, or databases.
- Atomic publication, temporary staging, resumability, or cleanup tooling.
- Overwrite, merge, append, or repair behavior.
- Concurrent downloads, queues, services, or workers.
- Configuration files, environment-variable configuration, or plugin systems.
- Machine-readable progress events or a graphical interface.

## Appendix A: Potential future enhancements

Consider these only after the MVP has been exercised:

- Add Common Crawl using its public source-WARC byte ranges.
- Add configurable output roots and deliberate overwrite or resume behavior.
- Add page bundles by discovering static HTML and CSS dependencies.
- Add WARC 1.1, multi-URL WARC packing, and cross-URL deduplication.
- Add CDXJ indexes if a concrete replay workflow requires them.
- Add a persisted inventory or manifest when auditability or restartability is required.
- Add bounded concurrency after measuring serial performance and source limits.
- Add richer progress, summaries, and machine-readable output.
- Deduplicate redirects if CDX metadata can reliably preserve their complete semantics.
- Preserve authentic request records if a source exposes them.
- Add atomic temporary-file replacement and stale-partial cleanup.

## Appendix B: Research notes

- [`cdx_toolkit`](https://github.com/commoncrawl/cdx_toolkit) supplies paged IA CDX iteration and exact-timestamp playback retrieval. Its documentation warns about underspecified date/result defaults, and smarter WARC revisit generation remains an upstream TODO.
- [`cdx_toolkit` WARC source](https://github.com/commoncrawl/cdx_toolkit/blob/main/cdx_toolkit/warc.py) shows that IA playback is reconstructed as an in-memory response and that source revisits are materialized as full responses. Fetch reuses that retriever but owns the response-versus-revisit decision.
- [`wayback2warc`](https://github.com/tmctmt/wayback2warc) demonstrates a compact direct IA downloader and useful URL-pattern behavior. It always writes full response records; its collapse option removes captures rather than preserving them as revisits. Its concurrency, proxy, rollover, arbitrary-lambda filtering, and large-file skip behavior are not needed here.
- The [IIPC WARC 1.0 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.0/) defines response records, identical-payload-digest revisits, `WARC-Refers-To`, and WARC files as sequences of records.
- [`warcio`](https://warcio.readthedocs.io/en/latest/) provides the standards-aware WARC 1.0 writer, per-record gzip, response construction, payload digests, and revisit construction needed by this MVP.
