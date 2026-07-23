# Archive Magic Fetch Implementation Handoff

## Objective

Implement the MVP described in [`ARCHITECTURE-FETCH.md`](ARCHITECTURE-FETCH.md).

The finished feature is a small Python CLI that:

1. Queries the Internet Archive CDX index for a URL pattern and optional date bounds.
2. Removes URL fragments and bare empty queries, collapses literal duplicate CDX rows, and groups distinct captures by CDX `urlkey`.
3. Writes one WARC 1.0 `.warc.gz` file per URL-key resource family.
4. Verifies each new IA source digest, decodes HTTP content encoding, and hashes normalized content.
5. Omits same-resource scheme/www/default-port redirects and Internet Archive
   playback substitutions.
6. Writes later source or normalized-content duplicates as revisit records without unnecessary downloads or duplicate bodies.

Treat the architecture memo as authoritative. Do not follow `DEPRECATED-ARCHITECTURE.md`, and do not implement `archive-magic-replay` concerns.

## Settled decisions

- Python 3.12 or newer, with Python 3.14 pinned for local development.
- `cdx_toolkit==0.9.39` for Internet Archive CDX discovery.
- `requests==2.34.2` for raw Wayback playback streaming.
- `warcio==1.8.1` for WARC writing.
- Standard-library `argparse` for the CLI.
- CLI shape:

  ```text
  archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
  ```

- Numeric CDX dates are passed through without a second date syntax.
- Missing start defaults to `1995`.
- Missing end defaults to the current UTC CDX timestamp.
- CDX results are materialized in memory and are not persisted separately.
- Internet Archive is the only source.
- Output root is always `./warcs/` for the MVP.
- Existing output files cause an error; do not overwrite, append, merge, or resume.
- Retrieval failures and digest mismatches warn, skip, and continue.
- Local path, file-writing, compression, or WARC serialization failures stop the command.
- Verified redirects that only switch HTTP/HTTPS, add or remove literal `www.`,
  or add or remove the matching default port are omitted when path and query
  are identical. Meaningful domain/path/query/nondefault-port redirects remain
  in the WARC.
- Internet Archive playback-generated redirects or capture substitutions are
  never stored as origin responses. Same-resource alias substitutions are
  omitted; meaningful substitutions warn and skip.
- A verified omitted source digest/status is reused across the URL-key group
  without another playback request. Undetectable `Location` changes are an
  accepted retrieval-minimization tradeoff because CDX does not expose that
  header.
- No page-dependency discovery. HTML, CSS, JavaScript, and images remain independent CDX URL-key resource families.
- No CDXJ, manifest, schema, database, staging transaction, concurrency, plugin system, or structured logging.

## Local development

Use `uv` to manage the interpreter, environment, lockfile, and dependencies. The
repository pins Python 3.14 in `.python-version` while retaining Python 3.12 as
the minimum supported runtime.

```sh
brew install uv
uv sync
uv run pytest
uv run archive-magic-fetch --help
```

`uv run` keeps `.venv` synchronized automatically, so manual activation and
direct `pip install` commands are unnecessary.

## Expected project layout

Create only this initial structure:

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
│       ├── retrieval.py
│       └── warc.py
└── tests/
    ├── test_discovery.py
    ├── test_paths.py
    ├── test_export.py
    └── test_retrieval.py
```

Do not introduce `core/`, `application/`, `adapters/`, `models/`, `services/`, or abstract base classes. Prefer small ordinary functions. A tiny local dataclass or named tuple for a canonical response reference is acceptable if it makes the digest map clearer.

## Suggested implementation sequence

### 1. Package and CLI shell

- Add `pyproject.toml` with the pinned runtime dependencies and a console-script entry point named `archive-magic-fetch`.
- Implement `parse_args()` and `main()` in `cli.py`.
- Apply defaults with:

  ```python
  date_start = args.start or "1995"
  date_end = args.end or current_utc_cdx_timestamp()
  ```

- Let `cdx_toolkit` validate numeric timestamp syntax.

### 2. Discovery

Implement in `discovery.py`:

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

Do not pass a limit, filter, or server-side collapse option. Collapse only literal duplicate CDX result rows locally. Remove fragments and bare empty query delimiters from `capture["url"]`, group distinct captures using `capture["urlkey"]`, and sort each group by `capture["timestamp"]`.

An empty result is successful and writes nothing. A CDX discovery failure is fatal.

### 3. Safe output paths

Implement the mapping in `paths.py`:

```text
./warcs/urlkey/<safe URL-key directories>/<stem>--<12-char URL-key hash>.warc.gz
```

Examples:

```text
com,example)/
  -> warcs/urlkey/com%2Cexample%29/index--<hash>.warc.gz

com,example)/images/logo.png
  -> warcs/urlkey/com%2Cexample%29/images/logo.png--<hash>.warc.gz
```

Write beneath `warcs/urlkey/`, mirror safe encoded URL-key path segments, and use the first 12 lowercase hexadecimal characters of SHA-256 over the CDX `urlkey` as the filename suffix. A URL key must never introduce an absolute path, `.` or `..`, an extra separator, or a path outside `./warcs/`.

After discovery and before any payload download:

- Compute every selected URL-key group's path.
- Reject two URL keys mapping to the same path.
- Reject any path that already exists.

### 4. WARC helpers

Keep WARC mechanics in `warc.py`:

- Open output files in exclusive-create mode.
- Use `WARCWriter(..., gzip=True, warc_version="1.0")`.
- Write a minimal initial `warcinfo` record.
- Convert the 14-digit CDX timestamp to the WARC 1.0 UTC form.
- For fetched responses, replace `WARC-Target-URI` and `WARC-Date` with the normalized CDX values before writing.
- Create identical-payload-digest revisit records with the current URL, current timestamp, payload digest, and `WARC-Refers-To` pointing to the canonical response record ID.
- For cross-target revisits, set `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` from the actual canonical response, not the current capture.
- Do not synthesize request records.

Be aware that `warcio.create_revisit_record()` supplies useful revisit fields but does not itself add the canonical response's `WARC-Record-ID` as `WARC-Refers-To`; add that header explicitly.

### 5. Export loop

Implement the per-URL-key-group state machine in `export.py`.

Use fresh source and normalized-content maps shared by each URL-key group:

```text
(CDX source digest, known status)
    -> normalized digest and canonical response

(normalized digest, known status)
    -> canonical response record ID, target URI, and capture date
```

For each capture in timestamp order:

1. Normalize a usable CDX digest to `sha1:` plus uppercase Base32.
2. If the same source digest and known status is already verified anywhere in the URL-key group, write a revisit using its normalized digest without fetching.
3. Otherwise stream exact-timestamp Wayback playback with automatic decoding disabled.
4. Detect an Internet Archive playback-generated redirect or substituted
   capture before digest verification. Omit a same-resource alias destination;
   warn and skip a meaningful destination.
5. Accept an archived 4xx/5xx when playback status matches the numeric CDX
   status; retry or skip a playback error that disagrees with CDX.
6. Verify either the raw or playback-decoded candidate against the CDX source digest.
7. Decode archived gzip/deflate content and repair representation-specific HTTP headers.
8. Replace the target URL and date with normalized CDX identity.
9. If a verified 3xx `Location` resolves to the same URL after only
   scheme/www/default-port normalization, omit it and remember its source
   digest/status.
10. Read the normalized `WARC-Payload-Digest` from the constructed response.
11. If normalized content already has a canonical response for this group/status, write a revisit; otherwise write the response.
12. Record both source-to-normalized and normalized-to-canonical mappings.

If the CDX digest is missing or malformed, download and normalize the response but do not seed the source-digest map. The normalized response may still become a revisit to existing content.

If all captures for a URL-key group are skipped, create no WARC—not even a `warcinfo`-only file.

### 6. Console messages

Use simple `print()` calls; warnings may go to stderr.

Required forms:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: capture unavailable
Omitted 13 canonical URL redirects
```

The displayed download hash is the last eight characters of the normalized payload digest.

## Important implementation traps

### Each unseen source digest is verified once

For source digests `A, A, B, B`, fetch and verify `A`, revisit `A`, fetch and verify `B`, then revisit `B`. If `A` and `B` decode to the same normalized content, only the first is stored as a response.

### CDX identity must replace synthesized identity

The CDX timestamp is the selected capture time and must replace any playback-derived date. The CDX URL with fragments and bare empty queries removed must replace playback identity.

### Do not use stock `cdxt warc`

The stock exporter retrieves duplicate captures and constructs records from automatically decoded `response.content`. Use Fetch's raw retriever so source verification happens before content normalization.

### Redirect classification is narrow and happens after retrieval

Do not change the CDX URL before requesting playback or before writing a
meaningful response. After retrieval, omit a verified 3xx only when its
resolved `Location` has the same hostname after stripping one literal `www.`,
the same path and query, and differs otherwise only by HTTP/HTTPS or a matching
default port. Preserve domain, path, query, and nondefault-port changes.

Wayback may manufacture a redirect or replay a nearby capture when the exact
one is unavailable. Detect its playback metadata before digest verification
and never write that result as an origin response.

CDX does not expose `Location`. Once one source digest/status is verified as an
omitted alias, reuse that classification across the URL-key group without
another download. This assumes an invisible destination change did not occur;
checking it would require retrieving every otherwise duplicate row.

### A failed capture is not a failed URL

Warn and continue to later captures. Do not fabricate placeholder responses. A valid partially populated WARC is preferable to aborting that URL's entire history.

### Do not catch local output failures as capture warnings

Network/retrieval and digest problems are skippable. Filesystem and WARC writer failures are fatal. Keep the exception boundary narrow enough to preserve that distinction.

## Test expectations

Use fake capture objects and temporary directories; deterministic tests must not contact the Internet Archive.

At minimum cover:

1. Date defaults and explicit date pass-through.
2. Discovery with explicit bounds and no result limit.
3. Fragment and bare-empty-query removal, literal-row collapse, URL-key grouping, and timestamp sorting.
4. Recognizable, safe, deterministic URL path mapping.
5. HTTP/HTTPS/`www` variants with one CDX URL key share one output path,
   while genuinely distinct URL keys (including query variants) remain
   separate.
6. Existing-file and generated-path collision failures before retrieval.
7. Response then revisit for a repeated same-target-URL digest/status, with only one fetch.
8. One fetch for the same digest/status across target URL spellings, followed by a cross-target revisit with correct canonical reference fields.
9. Raw and playback-decoded candidates verify correctly against CDX source digests.
10. Distinct gzip source digests can normalize to one response plus revisits.
11. A verified scheme/www/default-port redirect with unchanged path/query is
    omitted, summarized, and not refetched for the same source digest/status.
12. A meaningful domain/path/query/nondefault-port redirect is preserved.
13. An IA-generated alias substitution is omitted, while a meaningful
    substitution warns and is never written.
14. An archived 4xx/5xx matching CDX status is verified and preserved without
    retry, while a mismatched playback error is retried or skipped.
15. Missing digest downloads and normalizes but does not seed source identity.
16. Retrieval failure warns and processing continues.
17. Digest mismatch warns, skips, and does not seed either map.
18. An all-skipped URL produces no file.
19. Output parses with `warcio.ArchiveIterator` as WARC 1.0 and has the expected record order, URL, date, normalized digest, and `WARC-Refers-To` values.

After deterministic tests pass, an optional manual smoke test may make a very small Internet Archive request. Do not make remote availability part of the automated test result.

## Definition of done

- The package remains installable in a clean Python 3.12 environment.
- `uv run pytest` passes with the repository's pinned Python 3.14 interpreter.
- The `archive-magic-fetch` command exposes only the agreed arguments.
- All deterministic tests pass.
- A small manual run creates parseable WARCs beneath the stable URL-key directory tree.
- Duplicate group-level payload/status combinations produce revisits without a second fetch.
- Same-resource canonical redirects are omitted and summarized; meaningful
  redirects are preserved.
- Internet Archive playback substitutions are never represented as origin
  responses.
- Skipped captures are visible as warnings and do not stop unrelated work.
- No feature from the architecture's non-goals or future appendix is implemented.

## Workspace caution

Before editing, inspect `git status`. Preserve all pre-existing changes, including deleted or untracked files that are unrelated to the Fetch implementation. Do not restore, remove, stage, or commit them unless the user explicitly asks.
