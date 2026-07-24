# Archive Magic Fetch Architecture

**Status:** Implemented collection architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 23, 2026

## 1. Decision

`archive-magic-fetch` is a serial Python CLI that discovers Internet Archive
Wayback captures and exports a self-contained website collection:

```text
archive-magic/
├── archive-magic-fetch/
└── archives/
    └── example.com/
        ├── sources/
        │   └── wayback/
        │       └── 20260723T184501.123456Z/
        │           ├── query.json
        │           └── captures.cdx.gz
        ├── archive/
        │   ├── index.warc.gz
        │   ├── about.warc.gz
        │   └── posts/
        │       ├── index.warc.gz
        │       └── hello-world.warc.gz
        └── replay/
            └── index.cdxj
```

The three artifact areas have distinct ownership:

- `sources/` records the complete normalized discovery result returned by the
  high-level Wayback client.
- `archive/` contains WARC 1.0 response and revisit records written by Fetch.
- `replay/` indexes the exact compressed WARC bytes Fetch produced.

The source CDX is provenance, not a replay index. It has no offsets into the
local WARCs. The replay CDXJ is derived from WARC record metadata and compressed
byte ranges, not copied from the Internet Archive CDX.

Readable paths organize WARC storage but do not define capture identity.
Capture identity remains in CDX URL keys, WARC headers, target URIs, dates,
digests, and replay-index entries.

The implementation remains deliberately small:

- one Python process and serial retrieval;
- one Wayback session/client per command;
- public `wayback` APIs for discovery and playback;
- `warcio` for WARC construction;
- `cdxj-indexer` for final-byte replay indexing;
- a flat `src/archive_magic_fetch/` package; and
- no source-adapter hierarchy, database, resume system, or publication service.

## 2. CLI contract and data flow

The public command is unchanged:

```text
archive-magic-fetch URL_PATTERN [--start DATE] [--end DATE]
```

Numeric partial dates are passed through to `WaybackClient.search()`. Defaults
are explicit:

```python
date_start = args.start or "1995"
date_end = args.end or current_utc_cdx_timestamp()
```

The output root is the repository sibling `../archives`; there is no output
argument.

The successful flow is:

```text
parse arguments and apply date defaults
    -> derive and validate the collection name
    -> create one WaybackSession and WaybackClient
    -> fully materialize discovery
    -> if empty, print "No captures found" and exit successfully
    -> atomically publish the source acquisition
    -> collapse value-equal records and group by urlkey
    -> allocate readable WARC buckets
    -> preflight all WARC and replay targets
    -> export buckets and close their WARC streams
    -> generate and atomically publish replay/index.cdxj
    -> print the aggregate summary
    -> close the client/session
```

The aggregate summary is printed only after replay indexing succeeds. A fatal
indexing or replay-publication error therefore cannot follow an apparently
successful final summary. If every capture is omitted or skipped, no WARC or
replay index is created and the successful aggregate summary is still printed.

## 3. Wayback client and discovery

Fetch pins `wayback==0.5.1` and creates one descriptive session:

```python
session = WaybackSession(
    user_agent=(
        "archive-magic-fetch/0.1.0 "
        "(+https://github.com/RobJacobson/archive-magic)"
    )
)
```

One `WaybackClient` context owns that session across discovery and playback.
The library's endpoint-specific pacing remains in force. Fetch adds one bounded
rate-limit retry:

1. Sleep for `retry_after`, or 60 seconds when absent.
2. Retry the complete search or exact Memento operation once.
3. Propagate a second `RateLimitError`.

Discovery calls:

```python
client.search(
    url_pattern,
    from_date=date_start,
    to_date=date_end,
    resolve_revisits=False,
)
```

The lazy iterator is completely materialized. If the first attempt is rate
limited after yielding rows, those partial rows are discarded before the
whole search is retried.

After source provenance is saved, Fetch:

1. collapses only value-equal `CdxRecord` values;
2. groups records by normalized CDX `urlkey`; and
3. sorts captures within each group by aware timestamp.

The original discovery order, duplicates, and redirects remain present in the
source snapshot even though downstream export transforms that selection.

## 4. Collection naming

The requested pattern defines one collection before network access. Supported
exact, prefix, and leading-`*.` domain patterns use this normalization:

1. Extract one unambiguous host with `urllib.parse`.
2. Remove the trailing DNS dot and lowercase it.
3. Encode it using Python's built-in codec:

   ```python
   host.encode("idna").decode("ascii")
   ```

4. Lowercase the ASCII result and remove an exact leading `www.`.
5. Remove HTTP `:80` and HTTPS `:443`.
6. Retain any other port as `--port-<number>`.
7. Encode the result as one safe filesystem component.

Examples:

```text
https://Kevin.Burke.Dev/   -> kevin.burke.dev
http://www.example.com/*   -> example.com
*.example.com              -> example.com
https://example.com:443/*  -> example.com
https://example.com:8443/* -> example.com--port-8443
https://münich.example/*   -> xn--mnich-kva.example
```

A bare pattern's explicit port remains because no scheme identifies it as a
default. User information, embedded wildcards, missing hosts, and patterns
that cannot identify one website scope are rejected.

HTTP/HTTPS and ordinary `www`/apex spellings share a collection because
Wayback canonicalizes those variants into the same capture-family semantics.
The readable collection boundary follows that upstream identity model rather
than splitting one website by transport or conventional hostname spelling.

Collection naming never imports the third-party `idna` package. Consequently,
`cdxj-indexer`'s transitive `idna<3` pin cannot change collection names.
Collection names longer than the 240-byte application component cap fail
rather than being truncated and accidentally merging distinct websites.

## 5. Readable paths and filesystem limits

The path/query portion of each CDX URL key maps beneath `archive/`:

```text
/                    -> archive/index.warc.gz
/?view=full          -> archive/index%3Fview%3Dfull.warc.gz
/about               -> archive/about.warc.gz
/posts               -> archive/posts.warc.gz
/posts/              -> archive/posts/index.warc.gz
/posts/hello-world   -> archive/posts/hello-world.warc.gz
/images/logo.png     -> archive/images/logo.png.warc.gz
```

Unsafe values are percent-encoded as one component. Empty, dot, dot-dot,
separator, control, trailing-dot, and Windows-reserved-name cases cannot
escape or reshape the output root. Queries remain recognizable in the
filename so the collection remains understandable without an internal
identity hash.

Archive path components use an application limit of 240 encoded ASCII bytes,
including `.warc.gz`. Longer values are truncated deterministically without
cutting a `%XX` escape or appending a digest. Truncation collisions are safe
because filenames identify buckets, not records. Safety encoding is reapplied
after truncation so a cutoff cannot expose a literal trailing dot.

Preflight checks two independent filesystem limits using the nearest existing
ancestor:

- `PC_NAME_MAX` for every complete path component; and
- `PC_PATH_MAX` for the absolute path, including terminator space where
  applicable.

Unavailable, unlimited, or invalid platform values use conservative defaults:

```text
POSIX:   NAME_MAX 255 bytes, PATH_MAX 1024 bytes
Windows: NAME_MAX 255 ASCII characters, PATH_MAX 260 characters
```

The 240-byte application cap does not replace the actual `NAME_MAX` check, and
component checks do not replace `PATH_MAX`.

## 6. Collision buckets and preflight

`preflight_layout()` produces an ordered `ExportPlan` containing
`WarcBucket(path, urlkeys)` values.

Candidate paths are compared conservatively using:

- the safe encoder's canonical uppercase percent escapes;
- case folding;
- trailing-dot/space normalization; and
- the already applied component truncation and reserved-name encoding.

Filesystem-equivalent candidates share one WARC:

```text
/posts/       -> archive/posts/index.warc.gz
/posts/index  -> archive/posts/index.warc.gz
```

Preflight also detects when one planned WARC path would become the directory
ancestor of another, including after case folding or truncation. Descendant
groups are assigned to the ancestor WARC so a file/directory conflict cannot
surface after playback begins.

The lexically smallest safe candidate supplies the displayed bucket spelling.
Buckets sort by collection-relative path; URL keys within a bucket sort
lexically. Allocation is independent of discovery order.

Collision buckets retain resource identity through WARC metadata and distinct
CDXJ offsets. Sharing storage avoids arbitrary filename suffixes and prevents
rare naming collisions from aborting unattended exports, while resetting
deduplication at every URL key ensures storage sharing does not alter content
policy.

Before playback, preflight checks every planned WARC and
`replay/index.cdxj`. Existing final entries, broken final symlinks,
non-directory ancestors, component/path-limit violations, and other
uninspectable targets are fatal. Valid directory-symlink ancestors remain
supported.

An allocation collision is valid. An existing final file is not: Fetch does
not overwrite, append, merge, or repair output.

## 7. Source provenance

After complete successful discovery and before duplicate collapse, Fetch
publishes:

```text
sources/wayback/<acquisition>/
├── captures.cdx.gz
└── query.json
```

The acquisition ID is UTC with microseconds:

```text
20260723T184501.123456Z
20260723T184501.123456Z-2
```

### 7.1 Source CDX

`captures.cdx.gz` is UTF-8 classic CDX:

```text
CDX N b a m s k S
```

Its fields are:

```text
urlkey timestamp original mimetype statuscode digest length
```

Timestamps use 14-digit UTC form. Absent values use `-`. Discovery order,
value-equal duplicates, redirects, statusless rows, and non-ASCII URLs are
preserved. Whitespace-bearing tokens are rejected rather than serialized
ambiguously.

The gzip header has no temporary filename and uses the acquisition time as
its explicit `mtime`.

### 7.2 Query manifest

`query.json` is deterministic, schema-versioned JSON containing:

- exact URL pattern and date bounds;
- source identifier;
- microsecond acquisition time;
- installed Fetch and Wayback versions;
- CDX header and field schema;
- record count; and
- lowercase SHA-256 of the final compressed CDX bytes.

Package versions come from installed distribution metadata.

### 7.3 Publication

Both artifacts are written in a temporary sibling directory under
`sources/wayback/`. Publication uses a same-filesystem atomic no-replace
rename:

- Linux `renameat2(RENAME_NOREPLACE)`;
- macOS `renamex_np(RENAME_EXCL)`; or
- Windows' non-replacing rename behavior.

Fetch refuses to weaken directory publication on a platform without an
exclusive atomic rename primitive.

If a candidate ID already exists at publication time, including due to a
concurrent process, the completed temporary directory is retried as `-2`,
`-3`, and so forth. Publication cannot replace a directory created after an
earlier check. Failed attempts clean their temporary directory.

A published source acquisition remains valid provenance if preflight,
playback, WARC serialization, or replay indexing later fails.

## 8. Retrieval and WARC construction

For an unseen source signature, Fetch requests the exact original-mode
Memento:

```python
client.get_memento(
    capture,
    mode=Mode.original,
    exact=True,
    follow_redirects=False,
)
```

The response record uses the Memento's target URL, timestamp, source URI,
status, headers, and decoded semantic body. Representation-dependent headers
such as transfer/content encoding, source content length, and source digest
headers are removed. Fetch writes a new semantic `Content-Length`, and
`warcio` computes the payload digest over the stored body.

Known CDX 3xx rows are counted and omitted before playback. A statusless row
that plays back as 3xx is also counted and omitted. A known CDX/Memento status
mismatch warns and skips the capture. Fetch does not synthesize redirects,
unavailable-resource metadata, or broken-resource responses.

Approved Wayback playback/availability failures warn, count as playback
failures, and allow later captures to continue. Unexpected formats, repeated
rate limits, local filesystem errors, and serialization failures are fatal.

## 9. Shared-WARC export and deduplication

One bucket owns one lazily opened WARC stream:

1. Iterate its URL-key groups in sorted order.
2. Initialize fresh source and semantic maps for the current group.
3. Process captures in timestamp order.
4. Reuse the bucket writer for later groups.
5. Close the WARC after every assigned group completes.

The WARC receives one `warcinfo` record. If every assigned capture is omitted
or skipped, the stream is never opened and no WARC is created.

Deduplication never crosses a URL-key group boundary, even when two groups
share a WARC. Within a group:

- a successful CDX digest/status source signature can avoid later playback;
- statusless CDX rows can reuse a successful matching digest;
- semantic payload digest plus actual HTTP status selects response versus
  revisit; and
- maps are updated only after successful validation and serialization.

`export_all()` returns:

```text
ExportResult(summary, created_warcs)
```

It does not print the aggregate summary. `created_warcs` contains only WARCs
successfully closed during the current command and is the complete input to
replay indexing.

## 10. Replay CDXJ

Fetch pins `cdxj-indexer==1.4.6` and invokes its Python API with:

```python
CDXJIndexer(
    output=temporary_index,
    inputs=created_warcs,
    sort=True,
    records="response,revisit",
    dir_root=collection_root,
).process_all()
```

Only current-export WARCs are indexed. Other files beneath `archives/` are
never discovered recursively.

The resulting `replay/index.cdxj`:

- is sorted by replay URL key and timestamp;
- derives each replay URL key from the WARC record's `WARC-Target-URI`;
- includes collection-relative filenames such as
  `archive/posts/index.warc.gz`;
- records true compressed offsets and member lengths; and
- indexes response and revisit records only.

The Internet Archive source `urlkey` is not copied into replay entries.

Response entries normally contain target URL, response MIME, HTTP status,
payload digest, filename, offset, and length. Current revisit records have no
embedded HTTP response block, so their index entries may contain:

```json
{
  "mime": "warc/revisit",
  "digest": "sha1:...",
  "filename": "archive/posts/index.warc.gz",
  "offset": "...",
  "length": "..."
}
```

The indexer does not fabricate a missing status or response MIME. Revisit WARC
records retain `WARC-Payload-Digest`, `WARC-Refers-To`,
`WARC-Refers-To-Target-URI`, and `WARC-Refers-To-Date`, providing the digest
and canonical references required for replay resolution.

The index is written to a temporary sibling in `replay/`. Publication first
uses an atomic hard link to the final name and removes the temporary name; a
platform exclusive rename is the fallback. Either path has no-replace
semantics, so an index created after preflight is never overwritten.

An indexing or publication failure may leave completed WARCs, but never a
truncated final index. No WARC files means no replay directory or empty index.

## 11. Console and failure policy

Ordinary output remains compact:

```text
Starting https://example.com/images/logo.png
Downloaded 20170604120533 [a19f7c2e]
WARNING skipped 20190812143015 https://example.com/images/logo.png: unavailable
Summary: 235 selected; 209 responses; 17 revisits; 9 redirects omitted; 0 playback failures
```

The final summary reports selected rows, responses, revisits, deliberately
omitted redirects, and playback failures. Source-signature revisits that avoid
the network remain silent.

The CLI catches fatal errors, prints `ERROR: ...` to stderr, and returns 1.
Source publication, WARC writing, and replay publication are individually
safe, but the whole collection is intentionally not one transaction.

## 12. Project responsibilities and dependencies

The flat package contains:

| File | Responsibility |
| --- | --- |
| `cli.py` | Arguments, date defaults, client lifecycle, stage ordering, final summary/error boundary |
| `discovery.py` | Complete search, rate-limit retry, duplicate collapse, URL-key grouping |
| `paths.py` | Collection normalization, safe readable paths, filesystem limits, collision buckets, preflight |
| `publication.py` | Same-filesystem atomic no-replace file/directory publication |
| `provenance.py` | Source CDX and query-manifest serialization/publication |
| `retrieval.py` | Exact Memento playback and semantic response construction |
| `export.py` | Redirect/status policy, per-group deduplication, bucket export, aggregate result |
| `warc.py` | WARC dates, exclusive creation, response/revisit serialization |
| `replay.py` | Final-WARC CDXJ generation and publication |

Fetch supports Python 3.12 or newer; local development selects Python 3.14.
Pinned runtime dependencies are:

```text
cdxj-indexer==1.4.6
wayback==0.5.1
warcio==1.8.1
```

`cdxj-indexer` currently resolves `idna<3`. Collection naming is isolated from
that dependency through Python's built-in IDNA codec. Offline tests also
prepare an internationalized request through the locked Wayback/Requests
stack to verify outgoing punycode behavior.

## 13. Testing and acceptance

The deterministic suite uses real `CdxRecord` values, fake Wayback clients,
temporary collections, actual `warcio` parsing, and the real pinned CDXJ
indexer. It does not contact Internet Archive.

Acceptance covers:

- collection normalization, built-in IDNA, ports, and wildcard scopes;
- safe readable paths, query retention, component truncation, and collision
  allocation;
- independent `NAME_MAX` and `PATH_MAX` checks and fallbacks;
- existing targets, broken symlinks, and invalid ancestors;
- complete source CDX/manifest content, checksums, suffix allocation,
  concurrent publication, and cleanup;
- shared-WARC ownership with one `warcinfo` and fresh per-group maps;
- unchanged retrieval, redirect, status, retry, deduplication, and failure
  behavior;
- sorted response/revisit CDXJ semantics and nested filenames;
- exact offset/length selection of independently compressed WARC members;
- replay publication races and indexer failures; and
- final-summary ordering.

Routine validation is:

```bash
uv run --package archive-magic-fetch pytest
uv lock --check
uv run --package archive-magic-fetch archive-magic-fetch --help
git diff --check
```

The repository root is a uv workspace. Fetch keeps its own package metadata in
`archive-magic-fetch/pyproject.toml`, while every workspace member shares the
single repository-root `uv.lock`. Package-specific commands use
`--package archive-magic-fetch`; no nested lockfile is maintained.

## 14. Explicit non-goals

This implementation does not include:

- migration or rewriting of pre-existing archive data;
- overwrite, append, repair, merge, or resume behavior;
- output-root configuration;
- a generic archive-source interface or Common Crawl;
- source-WARC representation-byte preservation;
- redirect preservation or unavailable-capture metadata;
- cross-URL-key payload deduplication;
- concurrent retrieval;
- a replay server or pywb configuration;
- sharded or ZipNum replay indexes;
- a database or persistent naming registry;
- a general manifest framework; or
- atomic publication of the entire site collection.

## References

- [`wayback` client documentation](https://wayback.readthedocs.io/en/stable/)
- [`warcio` documentation](https://warcio.readthedocs.io/en/latest/)
- [`cdxj-indexer` 1.4.6 implementation](https://github.com/webrecorder/cdxj-indexer/blob/v1.4.6/cdxj_indexer/main.py)
- [pywb indexing documentation](https://pywb.readthedocs.io/en/latest/manual/indexing.html)
- [IIPC WARC 1.0 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.0/)
