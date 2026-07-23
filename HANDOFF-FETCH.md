# Archive Magic Fetch Implementation Handoff

## Objective

Implement the MVP described in [`ARCHITECTURE-FETCH.md`](ARCHITECTURE-FETCH.md).

The finished feature is a small Python CLI that:

1. Queries the Internet Archive CDX index for a URL pattern and optional date bounds.
2. Groups captures by exact resource URL.
3. Writes one WARC 1.0 `.warc.gz` file per exact URL.
4. Downloads the first verified occurrence of each payload digest within that URL.
5. Writes later same-URL duplicates as revisit records without downloading them again.

Treat the architecture memo as authoritative. Do not follow `DEPRECATED-ARCHITECTURE.md`, and do not implement `archive-magic-replay` concerns.

## Settled decisions

- Python 3.12 or newer, with Python 3.14 pinned for local development.
- `cdx_toolkit==0.9.39` for Internet Archive CDX discovery and capture retrieval.
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
- All 3xx captures are downloaded and written as full responses. They are never revisit-deduplicated and never seed the digest map.
- No page-dependency discovery. HTML, CSS, JavaScript, and images are independent exact URL resources.
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
│       └── warc.py
└── tests/
    ├── test_discovery.py
    ├── test_paths.py
    └── test_export.py
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

Do not pass a limit, filter, or collapse option. Group captures using `capture["url"]`, not `urlkey`, and sort each group by `capture["timestamp"]`.

An empty result is successful and writes nothing. A CDX discovery failure is fatal.

### 3. Safe output paths

Implement the mapping in `paths.py`:

```text
./warcs/<scheme>/<host>/<URL directories>/<stem>--<12-char URL hash>.warc.gz
```

Examples:

```text
https://example.com/
  -> warcs/https/example.com/index--<hash>.warc.gz

https://example.com/images/logo.png?v=2
  -> warcs/https/example.com/images/logo.png--<hash>.warc.gz
```

The hash is the first 12 lowercase hexadecimal characters of SHA-256 over the exact URL bytes. Encode every scheme, host, and path component as one filesystem-safe segment. A URL must never introduce an absolute path, `.` or `..`, an extra separator, or a path outside `./warcs/`.

After discovery and before any payload download:

- Compute every selected URL's path.
- Reject two URLs mapping to the same path.
- Reject any path that already exists.

### 4. WARC helpers

Keep WARC mechanics in `warc.py`:

- Open output files in exclusive-create mode.
- Use `WARCWriter(..., gzip=True, warc_version="1.0")`.
- Write a minimal initial `warcinfo` record.
- Convert the 14-digit CDX timestamp to the WARC 1.0 UTC form.
- For fetched responses, replace `WARC-Target-URI` and `WARC-Date` with the exact CDX values before writing.
- Create identical-payload-digest revisit records with the current URL, current timestamp, payload digest, and `WARC-Refers-To` pointing to the canonical response record ID.
- Do not synthesize request records.

Be aware that `warcio.create_revisit_record()` supplies useful revisit fields but does not itself add the canonical response's `WARC-Record-ID` as `WARC-Refers-To`; add that header explicitly.

### 5. Export loop

Implement the per-URL state machine in `export.py`.

Use a fresh map for each exact URL:

```text
digest -> canonical response record ID and capture date
```

For each capture in timestamp order:

1. Normalize a usable CDX digest to `sha1:` plus uppercase Base32.
2. If it is a non-redirect and the digest is already in the map, write a revisit without fetching.
3. Otherwise call `capture.fetch_warc_record()`.
4. Replace the synthesized target URL and date with CDX identity.
5. Read the calculated `WARC-Payload-Digest` from the constructed response.
6. If a usable CDX digest disagrees, warn and skip.
7. Lazily create the output WARC only when the first response is ready to write.
8. Write the response and log the successful download.
9. Add a non-redirect response to the map with `setdefault()` so the first verified response stays canonical.

If the CDX digest is missing or malformed, download and write a full response; its calculated digest may seed the map. Do not retroactively turn that fetched response into a revisit.

If all captures for a URL are skipped, create no WARC—not even a `warcinfo`-only file.

### 6. Console messages

Use simple `print()` calls; warnings may go to stderr.

Required forms:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: capture unavailable
```

The displayed download hash is the last eight characters of the normalized payload digest.

## Important implementation traps

### `fetch_warc_record()` is used per unseen digest

It is not limited to the first capture of a URL. For digests `A, A, B, B`, fetch `A`, revisit `A`, fetch `B`, revisit `B`.

### CDX identity must replace synthesized identity

For Internet Archive playback, `cdx_toolkit` synthesizes a response record and may derive `WARC-Date` from an archived HTTP `Date` header. The CDX timestamp is the selected capture time and must replace it.

### Do not use stock `cdxt warc`

The stock exporter retrieves duplicate captures and materializes them as full responses. Use `CaptureObject.fetch_warc_record()` only after the per-URL digest decision.

### Redirect digests are unsafe deduplication keys

Different redirects often have identical empty payloads but different `Location` headers. Always fetch and write 3xx captures as full responses.

### A failed capture is not a failed URL

Warn and continue to later captures. Do not fabricate placeholder responses. A valid partially populated WARC is preferable to aborting that URL's entire history.

### Do not catch local output failures as capture warnings

Network/retrieval and digest problems are skippable. Filesystem and WARC writer failures are fatal. Keep the exception boundary narrow enough to preserve that distinction.

## Test expectations

Use fake capture objects and temporary directories; deterministic tests must not contact the Internet Archive.

At minimum cover:

1. Date defaults and explicit date pass-through.
2. Discovery with explicit bounds and no result limit.
3. Exact-URL grouping and timestamp sorting.
4. Recognizable, safe, deterministic URL path mapping.
5. Query-string and HTTP/HTTPS path separation.
6. Existing-file and generated-path collision failures before retrieval.
7. Response then revisit for a repeated same-URL digest, with only one fetch.
8. Independent fetches for the same digest at different URLs.
9. A new digest later in one URL uses the same retrieval path as the first digest.
10. Redirect captures always produce full responses.
11. Missing digest downloads a full response and seeds the calculated digest.
12. Retrieval failure warns and processing continues.
13. Digest mismatch warns, skips, and does not seed the map.
14. An all-skipped URL produces no file.
15. Output parses with `warcio.ArchiveIterator` as WARC 1.0 and has the expected record order, URL, date, digest, and `WARC-Refers-To` values.

After deterministic tests pass, an optional manual smoke test may make a very small Internet Archive request. Do not make remote availability part of the automated test result.

## Definition of done

- The package remains installable in a clean Python 3.12 environment.
- `uv run pytest` passes with the repository's pinned Python 3.14 interpreter.
- The `archive-magic-fetch` command exposes only the agreed arguments.
- All deterministic tests pass.
- A small manual run creates parseable WARCs beneath the mirrored URL directory tree.
- Duplicate same-URL payloads produce revisits without a second fetch.
- Skipped captures are visible as warnings and do not stop unrelated work.
- No feature from the architecture's non-goals or future appendix is implemented.

## Workspace caution

Before editing, inspect `git status`. Preserve all pre-existing changes, including deleted or untracked files that are unrelated to the Fetch implementation. Do not restore, remove, stage, or commit them unless the user explicitly asks.
