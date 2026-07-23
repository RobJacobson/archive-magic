# Archive Magic Fetch Architecture

**Status:** MVP architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 23, 2026

## 1. Decision

`archive-magic-fetch` is a small Python CLI that exports Internet Archive captures into WARC 1.0 files.

The MVP does only three things:

1. Query the Internet Archive CDX index for a URL pattern and time range.
2. Fetch each distinct payload needed by the selected captures.
3. Write one gzip-compressed WARC file for each CDX URL-key resource family.

A resource family is the set of original URL variants represented by one Internet Archive CDX `urlkey`/SURT, such as HTTP, HTTPS, `www`, and explicit-default-port spellings of the same page path. Each WARC retains the exact selected CDX target URL on every preserved record, except for removing fragments and bare empty queries, but does not create a separate file for each spelling. Fetch does not attempt to discover which assets belong to an HTML page or bundle page dependencies together.

The implementation follows KISS and YAGNI:

- One Python process.
- One archive source: the Internet Archive.
- One WARC profile: WARC 1.0.
- One WARC and one shared digest namespace per CDX URL key.
- Stock `cdx_toolkit` for CDX discovery; a small Fetch-owned IA playback retriever.
- `requests` for raw playback streaming.
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

Each CDX URL-key group maps to one stable WARC path based only on that key:

```text
warcs/
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
2. CDX URL-key path segments become nested directories.
3. The URL-key authority/SURT component is a safe encoded directory.
4. The final path segment becomes the readable filename stem.
5. A root URL or path ending in `/` uses `index` as the stem.
6. The stem ends with the first 12 lowercase hexadecimal characters of SHA-256 over the CDX `urlkey`, followed by `.warc.gz`.
7. Query strings are not written literally; URL-key differences are distinguished by the hash.
8. Every filesystem component is encoded into a safe single segment. Empty, `.`, `..`, separators, control characters, and platform-unsafe characters cannot escape or reshape the output root.

The URL-key hash prevents common collisions while keeping paths recognizable. Fetch also checks the complete set of generated paths before downloading payloads and fails if two selected groups map to the same path.

An existing target WARC is an error. The MVP does not overwrite, append, merge, or resume.

## 4. Data flow

```text
CLI arguments
    -> apply explicit date defaults
    -> query Internet Archive through cdx_toolkit
    -> materialize CDX captures in memory
    -> remove fragments and bare empty queries from capture URLs
    -> collapse literal duplicate CDX rows
    -> group by CDX urlkey
    -> sort each group by capture timestamp
    -> preflight output paths
    -> export each URL-key group independently
         -> create source and normalized digest maps for the URL-key group
         -> fetch each unseen source payload/status combination
         -> reject Internet Archive playback substitutions
         -> verify raw source bytes against the CDX digest
         -> decode HTTP content encoding and repair headers
         -> omit same-resource scheme/www/default-port redirects
         -> write responses or revisits
         -> close the WARC
```

CDX results exist only in memory. Fetch does not persist a source inventory, CDX file, CDXJ, manifest, database, or checkpoint.

Grouping uses the CDX `urlkey`/SURT so straightforward URL variants of the same resource share one WARC and one digest namespace. Every response or revisit still carries its normalized capture `url` as `WARC-Target-URI`. A revisit may refer to a canonical response whose target URI is another spelling in the group.

Fragments and bare empty query delimiters are removed before grouping output,
playback retrieval, path selection, and WARC identity. A fragment never reaches
an HTTP server, and a bare `?` contains no query parameters while causing
unreliable exact Wayback playback. Nonempty queries and other URL components
remain intact on individual records.

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
- Literal duplicate CDX rows are collapsed; every distinct returned row is retained.
- URL fragments and bare empty queries are removed from returned capture URLs.
- Captures are grouped by CDX `urlkey` and ordered by timestamp.

An empty result is a successful command that reports that no captures were found and writes no files. A failure to complete CDX discovery stops the command because Fetch cannot know what should be exported.

## 6. Per-group export

Each CDX URL key is an independent processing unit. Its WARC contains all successfully preserved captures for every target-URL variant in that group, in chronological order.

Source and normalized digest state are shared across the URL-key group:

```text
(CDX source digest, known CDX status)
    -> normalized payload digest and canonical response

(normalized payload digest, known response status)
    -> canonical response record ID, target URI, and capture date
```

The state is discarded when the WARC closes. A verified source digest/status is downloaded once regardless of which target-URL spelling first exposes it. Different source digests are each downloaded once, but may normalize to one canonical payload/status. Status remains part of both signatures so identical bodies served as, for example, 200 and 404 are not conflated.

The core loop is:

```python
for urlkey, captures in capture_groups:
    source_by_signature = {}
    source_by_digest = {}
    content_by_signature = {}
    content_by_digest = {}
    omitted_source_signatures = set()
    omitted_alias_redirects = 0
    writer = None

    log_start(captures[0].url)

    for capture in captures:
        expected = normalize_digest(capture.get("digest"))
        status = usable_cdx_status(capture)

        if (expected, status) in omitted_source_signatures:
            omitted_alias_redirects += 1
            continue

        source_match = find_seen_source_digest_status(expected, status)
        if source_match is not None:
            write_revisit(writer, capture, source_match.normalized_digest,
                          source_match.canonical)
            continue

        try:
            response = fetch_raw_playback(capture)
        except PlaybackSubstitution as substitution:
            if is_same_resource_alias(capture.url, substitution.target_url):
                remember_omitted_signature(expected, status)
                omitted_alias_redirects += 1
            else:
                warn_and_skip(capture, substitution)
            continue

        try:
            verify_source_digest(response, expected)
            response = decode_content_encoding_and_repair_headers(response)
            use_cdx_identity(response, capture)
            normalized = calculated_payload_digest(response)
        except CaptureRetrievalError as error:
            warn_and_skip(capture, error)
            continue

        if is_verified_alias_redirect(response):
            remember_omitted_signature(expected, response.status)
            omitted_alias_redirects += 1
            continue

        if writer is None:
            writer = open_new_warc(urlkey)

        canonical = find_seen_normalized_content(normalized, response.status)
        if canonical is None:
            canonical = write_response(writer, response)
        else:
            write_revisit(writer, capture, normalized, canonical)
        log_download(capture, normalized)

        remember_source_and_content_digests(
            capture, expected, normalized, response.status, canonical
        )

    close_if_open(writer)
    log_omitted_alias_redirects(omitted_alias_redirects)
```

This pseudocode describes policy rather than required names or exception types.

### 6.1 Retrieval

For every capture whose usable CDX source digest and known status have not been seen in its URL-key group, Fetch makes one exact-timestamp Wayback playback request. Source-revisit rows with status `-` may reuse any already verified occurrence of that source digest in the group.

The indexed CDX status distinguishes an archived origin error from a playback
service failure. A playback 4xx or 5xx that matches the numeric CDX status is a
valid archived response and proceeds to source-digest verification. A retryable
playback status such as 500 is retried and skipped only when it does not match
the indexed status. This preserves intentionally captured error responses
without mistaking an unrelated Wayback error page for archived content.

Wayback can replace the selected capture with a playback-generated redirect or
another capture. Fetch detects the explicit `X-Archive-Redirect-Reason` signal
and the indexed-redirect/replayed-response form exposed by Memento `Link`
metadata before digest verification. Such a response is evidence about
playback, not an archived origin response, and is never written to the WARC.
Its original-URL destination is used only to decide whether to count it as an
omitted same-resource alias or warn that the requested capture could not be
retrieved faithfully.

Fetch streams the raw playback body through `requests` with automatic decoding disabled. Wayback can apply transfer gzip to an originally uncompressed capture, while an archived response can itself use HTTP `Content-Encoding`. Fetch compares both raw and playback-decoded candidates with the CDX digest to identify the source representation.

After source verification, Fetch decodes archived `gzip`, `x-gzip`, and `deflate` content into semantic payload bytes. It removes `Content-Encoding` and transfer framing, replaces `Content-Length`, and removes representation validators such as `ETag`, `Content-MD5`, and `Digest` when the represented bytes changed. Unsupported encodings are skipped rather than written inconsistently.

Fetch does not use `CaptureObject.fetch_warc_record()` or the stock `cdxt warc` exporter for IA payload retrieval. Both construct records from Requests' automatically decoded `response.content`, which cannot distinguish archived content encoding from Wayback transfer encoding.

The selected CDX row remains authoritative for the target URL and capture timestamp, except that its fragment and bare empty query are removed. Fetch replaces the synthesized response record's `WARC-Target-URI` and `WARC-Date` with those normalized CDX values before writing it.

### 6.2 Digests

Internet Archive CDX digests are normalized into the form used by `warcio`, normally `sha1:` followed by uppercase Base32. They are source digests over the archived representation and are not necessarily the digest written to the normalized WARC.

Each downloaded capture has two digest roles:

- The source digest verifies raw or playback-decoded Wayback bytes against CDX before transformation.
- The normalized digest is `WARC-Payload-Digest` over the decoded content actually written by `warcio`.

A matching usable CDX digest verifies the source response. A missing or malformed CDX digest forces a download but cannot seed the source-digest map. If neither raw nor playback-decoded bytes match a usable CDX digest, Fetch warns and skips the capture. A skipped or mismatched capture never adds digest state.

Two distinct source digests may normalize to the same content digest. Each new source digest must be downloaded once for verification; after normalization, the later capture becomes a revisit to the existing canonical content. Subsequent rows with either verified source digest avoid playback entirely.

### 6.3 Revisits

A later capture with a source digest and known CDX status already verified anywhere in the URL-key group becomes a WARC 1.0 `revisit` record without another playback request. A CDX source-revisit row with status `-` may reuse any already verified canonical response for that source digest. Its revisit uses the associated normalized payload digest, not the CDX source digest.

The revisit uses the identical-payload-digest profile and includes:

- The current capture's normalized target URL.
- The current capture timestamp.
- The normalized payload digest.
- `WARC-Refers-To` naming the earlier canonical response record.
- `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` naming the
  canonical response's actual target URI and date.
- No duplicate payload body.

The canonical response is always written before its revisits. Cross-target revisits retain the current URL as `WARC-Target-URI` while their reference fields accurately identify the earlier canonical URL spelling.

Not fetching a duplicate means Fetch cannot observe HTTP header changes that are absent from CDX. This loss of per-capture header fidelity is an accepted MVP tradeoff.

### 6.4 Redirects and retrieval minimization

Fetch omits a verified 3xx response when its source URL and resolved `Location`
differ only by:

- HTTP versus HTTPS;
- one literal leading `www.` label; or
- a matching default port (`:80` for HTTP or `:443` for HTTPS).

The path and query must match exactly. Nondefault ports, user information,
different domains, and any path or query change are not aliases. This narrow
test is deliberately less aggressive than full SURT normalization: it removes
the protocol/host-spelling redirects that do not improve playback while
preserving redirects that express site navigation or migration.

Internet Archive playback-generated redirects and substitutions are never
written as if they came from the origin. A playback substitution whose target
is a same-resource alias is omitted; one whose target changes the domain, path,
or query warns and skips because Fetch does not possess the indexed origin
response.

Meaningful verified origin redirects are preserved. Their first payload/status
occurrence is written as a full response, and later captures with the same
verified source digest and status become revisits. A visible status change,
such as 301 to 302, forces a new download.

CDX does not expose the `Location` header. After Fetch verifies and classifies a
same-resource alias redirect, it remembers that source digest/status and omits
later occurrences without retrieving them. This assumes an otherwise
indistinguishable redirect destination remains stable; detecting an invisible
`Location` change would require downloading every row. The omission count is
reported once per URL-key group instead of warning for each alias capture.

## 7. WARC profile

Every output file:

- Uses WARC 1.0.
- Uses record-at-a-time gzip compression through `warcio.WARCWriter(gzip=True)`.
- Begins with a minimal `warcinfo` record identifying Archive Magic and WARC 1.0.
- Contains response and revisit records for one CDX URL-key group, potentially including several exact target URLs; same-resource canonical redirects are excluded.
- Preserves the normalized CDX target URL and capture timestamp.
- Includes payload digests.
- Writes canonical responses before dependent revisits.
- Includes `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` on
  cross-target revisits. These fields are standardized by WARC 1.1; the MVP
  uses them as extension fields in WARC 1.0 and accepts that interoperability
  tradeoff to avoid duplicate IA retrievals and bodies.
- Contains no fabricated request records.

If every capture for a URL-key group is skipped, Fetch creates no WARC for that group. It does not create a `warcinfo`-only file or an unavailable placeholder record.

## 8. Failure and logging policy

Remote capture failures degrade gracefully:

- An unavailable capture logs a warning and is skipped.
- An exhausted retrieval failure logs a warning and is skipped.
- An Internet Archive playback substitution to a meaningfully different URL
  logs a warning and is skipped.
- A playback HTTP error that disagrees with the indexed CDX status logs a
  warning and is skipped.
- A payload-digest mismatch logs a warning and is skipped.
- A same-resource scheme/www/default-port redirect is omitted without a
  per-capture warning and contributes to the group's omission summary.
- Other captures and URL-key groups continue processing.

Fetch stops for errors that prevent it from safely continuing the local job:

- Invalid CLI input.
- Incomplete CDX discovery.
- Two selected URL-key groups mapping to the same output path.
- An existing target WARC.
- Directory creation, file writing, compression, or WARC serialization failure.

Fetch does not create a synthetic broken-resource response. Absence of a record naturally produces a missing resource during replay.

Console output stays intentionally small:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: capture unavailable
Omitted 13 canonical URL redirects
```

Required messages are:

- `Starting ...` once for each URL-key group, with a variant count when needed.
- `Downloaded ... [hash]` for each successful network download, using the last eight characters of the normalized payload digest.
- `WARNING ...` for each skipped capture other than an expected canonical URL
  alias.
- `Omitted N canonical URL redirect(s)` once after a group with omitted aliases.

No logging framework, structured event protocol, progress bar, log file, or verbosity configuration is required.

## 9. Minimal project layout

```text
archive-magic-fetch/
├── .gitignore
├── .python-version
├── pyproject.toml
├── uv.lock
├── src/
│   └── archive_magic_fetch/
│       ├── __init__.py
│       ├── cli.py
│       ├── discovery.py
│       ├── paths.py
│       ├── export.py
│       ├── retrieval.py
│       └── warc.py
└── tests/
    ├── test_discovery.py
    ├── test_paths.py
    ├── test_export.py
    └── test_retrieval.py
```

The Python modules have narrow responsibilities:

| File | Responsibility | Principal functions |
| --- | --- | --- |
| `cli.py` | Arguments, date defaults, console entry point | `parse_args()`, `main()` |
| `discovery.py` | IA CDX query, URL normalization, literal-row collapse, URL-key grouping | `discover()`, `group_captures()` |
| `paths.py` | Safe deterministic URL-key-to-WARC paths and preflight checks | `urlkey_warc_path()`, `preflight_paths()` |
| `export.py` | Per-group source/content digest maps, skip policy, simple messages | `export_all()`, `export_group()` |
| `retrieval.py` | Raw IA playback verification, decoding, header repair | `fetch_normalized_ia_response()` |
| `warc.py` | WARC-specific response preparation and record writing | `prepare_response()`, `open_new_warc()`, `write_response()`, `write_revisit()` |

No `core/`, `application/`, `adapters/`, `models/`, `interfaces/`, or `services/` hierarchy is needed. A new file or abstraction should be introduced only when existing code has two concrete responsibilities that cannot remain clear as small functions.

The package supports Python 3.12 or newer. Local development selects Python 3.14
through `.python-version`, and `uv.lock` records the reproducible project
dependency resolution. The direct runtime dependencies remain exactly pinned:

```text
cdx_toolkit==0.9.39
requests==2.34.2
warcio==1.8.1
```

`pytest>=8` belongs to the default `dev` dependency group in `pyproject.toml`.
`uv` manages the local interpreter, `.venv`, lockfile, and development
dependencies; it is a development workflow rather than part of Fetch's runtime
architecture.

## 10. Testing and acceptance

Tests use fake `CaptureObject`-like objects and temporary directories. Routine tests do not contact the Internet Archive.

Routine local development is activation-free:

```bash
uv run pytest
uv run archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

`uv run` creates or synchronizes `.venv` before invoking the command. A clean
acceptance check also runs `uv lock --check`. Generated environments, caches,
build products, and `warcs/` output remain excluded by `.gitignore`.

The MVP is accepted when tests demonstrate:

1. Missing date bounds expand to 1995 and/or the present.
2. Discovery passes explicit bounds and no hidden result limit.
3. Captures have fragments and bare empty queries removed, literal duplicate rows collapse, and distinct rows group by CDX `urlkey` in timestamp order.
4. URL-key group paths map safely and deterministically beneath `./warcs/`.
5. Existing targets and path collisions fail before downloads begin.
6. Raw and transfer-decoded source candidates are checked against the CDX digest.
7. Gzip/deflate source representations normalize to decoded WARC payloads with repaired headers.
8. Different source digests that decode to one content digest produce one response and revisits.
9. A later verified source-digest/status occurrence becomes a revisit without fetching.
10. The same digest/status at another URL spelling becomes a cross-target revisit without fetching, with correct canonical reference fields.
11. Verified scheme/www/default-port redirects with identical path/query are
    omitted and summarized, and a repeated omitted source signature is not
    fetched again.
12. A redirect that changes domain, path, query, or a nondefault port is
    preserved.
13. An Internet Archive playback-generated alias substitution is omitted,
    while a meaningful substitution warns and is never written as origin data.
14. An archived 4xx/5xx matching CDX status is verified and preserved, while
    a mismatched playback error is retried or skipped.
15. Retrieval failures and digest mismatches warn, skip, and continue.
16. An all-skipped URL produces no WARC.
17. `warcio` can parse each output as WARC 1.0 with the expected response/revisit order, target URL, timestamps, and canonical references.

One small, manually invoked Internet Archive smoke test may verify current upstream behavior. Remote availability is not part of the deterministic test suite.

## 11. Explicit non-goals

The MVP does not include:

- Replay or any `archive-magic-replay` code.
- Common Crawl or other archives.
- HTML dependency discovery or page bundles.
- Exact preservation of HTTP content-encoding bytes in output WARCs.
- Cross-URL-key deduplication.
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
- Add WARC 1.1 if strict standardized cross-target-reference semantics or
  broader replay interoperability requires it.
- Add CDXJ indexes if a concrete replay workflow requires them.
- Add a persisted inventory or manifest when auditability or restartability is required.
- Add bounded concurrency after measuring serial performance and source limits.
- Add richer progress, summaries, and machine-readable output.
- Preserve authentic request records if a source exposes them.
- Add atomic temporary-file replacement and stale-partial cleanup.

## Appendix B: Research notes

- [`cdx_toolkit`](https://github.com/commoncrawl/cdx_toolkit) supplies paged IA CDX iteration and documents the exact-timestamp playback URL shape. Its documentation warns about underspecified date/result defaults, and smarter WARC revisit generation remains an upstream TODO.
- [`cdx_toolkit` WARC source](https://github.com/commoncrawl/cdx_toolkit/blob/main/cdx_toolkit/warc.py) shows that IA playback is reconstructed from automatically decoded `response.content` and that source revisits are materialized as full responses. Fetch uses stock `cdx_toolkit` for discovery but owns raw playback retrieval, source verification, content normalization, and response-versus-revisit decisions.
- [`wayback2warc`](https://github.com/tmctmt/wayback2warc) demonstrates a compact direct IA downloader and useful URL-pattern behavior. It always writes full response records; its collapse option removes captures rather than preserving them as revisits. Its concurrency, proxy, rollover, arbitrary-lambda filtering, and large-file skip behavior are not needed here.
- The [IIPC WARC 1.0 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.0/) defines response records, identical-payload-digest revisits, `WARC-Refers-To`, and WARC files as sequences of records.
- [`warcio`](https://warcio.readthedocs.io/en/latest/) provides the standards-aware WARC 1.0 writer, per-record gzip, response construction, payload digests, and revisit construction needed by this MVP.
