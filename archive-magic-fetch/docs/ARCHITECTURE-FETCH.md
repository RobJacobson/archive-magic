# Archive Magic Fetch Architecture

**Status:** Implemented collection architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 24, 2026

## 1. Decision

`archive-magic-fetch` is a Python CLI that discovers Internet Archive Wayback
captures and exports a self-contained website collection:

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
        ├── replay/
        │   └── index.cdxj
        └── website/
            ├── index.html
            ├── about/
            │   └── index.html
            └── css/
                └── style.css
```

The artifact areas have distinct ownership:

- `sources/` records the complete normalized discovery result returned by the
  high-level Wayback client.
- `archive/` contains WARC 1.0 response and revisit records written by Fetch
  when `--warc` is enabled.
- `replay/` indexes the exact compressed WARC bytes Fetch produced.
- `website/` contains optional loose website bodies written when `--files` is
  enabled.

The source CDX is provenance, not a replay index. It has no offsets into the
local WARCs. The replay CDXJ is derived from WARC record metadata and compressed
byte ranges, not copied from the Internet Archive CDX.

Readable paths organize WARC storage but do not define capture identity.
Capture identity remains in CDX URL keys, WARC headers, target URIs, dates,
digests, and replay-index entries.

The implementation remains deliberately small:

- one Python process with bounded concurrent memento retrieval;
- serial discovery and WARC writing, with asynchronous fetch progress;
- public `wayback` APIs for discovery and playback;
- `warcio` for WARC construction;
- `cdxj-indexer` for final-byte replay indexing;
- a flat `src/archive_magic_fetch/` package; and
- no source-adapter hierarchy, database, checkpoint store, or publication
  service; committed WARC records are the durable resume state.

## 2. CLI contract and data flow

The public command is:

```text
archive-magic-fetch URL_PATTERN
  [--start DATE] [--end DATE]
  [--warc {none,latest,all}]
  [--files {none,latest,all}]
  [--rewrite-local]
  [--concurrency N]
```

| Flag | Values | Default |
| --- | --- | --- |
| `--warc` | `none`, `latest`, `all` | `all` |
| `--files` | `none`, `latest`, `all` | `none` |
| `--rewrite-local` | flag | off |
| `--concurrency` | integer ≥ 1 | `8` |

`--concurrency 1` restores serial memento downloads for diagnostics. Values
above 8 mostly queue behind the Wayback client's independent 8 requests/second
memento pacing. The value is a ceiling: adaptive admission begins at two
in-flight requests, backs down under pressure, and ramps toward the ceiling
after sustained success.

The two axes are independent. Default behavior remains full WARC history plus
replay CDXJ with no loose files. `--warc none --files none` exits successfully
with `Nothing to do: both --warc and --files are none` and performs no
discovery. `--rewrite-local` requires `--files latest` or `--files all`
(usage error otherwise) and does not enable files mode by itself.

Numeric partial dates are passed through unchanged to `WaybackClient.search()`,
which forwards them to the Internet Archive CDX `from`/`to` parameters. Those
bounds are inclusive at the supplied precision (`YYYY` … `YYYYMMDDhhmmss`), so
`--end 2003` includes captures through the end of 2003. Fetch does not rewrite,
pad, or reinterpret them. Defaults are explicit:

```python
date_start = args.start or "1995"
date_end = args.end or current_utc_cdx_timestamp()
```

The output root is the repository sibling `../archives`; there is no output
argument.

The successful flow is:

```text
parse arguments and apply date defaults
    -> if warc=none and files=none: message + exit 0
    -> derive and validate the collection name
    -> create one WaybackSession and WaybackClient
    -> fully materialize discovery
    -> if empty, print "No captures found" and exit successfully
    -> atomically publish the source acquisition (full discovery set)
    -> collapse value-equal records and group by urlkey
    -> build warc_selection and files_selection from output modes
    -> if warc enabled: allocate WARC buckets and preflight WARC/replay
    -> if files enabled: plan website/ paths (query folding + newest-wins)
         and preflight remaining targets
    -> if warc enabled: export WARCs; generate replay/index.cdxj
    -> if files enabled: write loose website bodies
    -> if --rewrite-local and at least one file was written: rewrite under website/
    -> print aggregate summary
    -> close the client/session
```

Selection is an export transform. Provenance always stores the full CDX result
for the query window, not the post-`latest` subset.

`latest` keeps exactly one capture per urlkey group:

1. newest capture whose CDX status is `200`; else
2. newest capture whose status is present and not `3xx`; else
3. omit the group.

Shared captures needed by both outputs are fetched once through a retrieval
cache and fanned out to the WARC and loose-file writers.

The aggregate summary is printed only after enabled output stages succeed. A
fatal indexing or replay-publication error therefore cannot follow an apparently
successful final summary. If every WARC capture is omitted or skipped, no WARC
or replay index is created and the successful aggregate summary is still printed.

## 3. Wayback client and discovery

Fetch pins `wayback==0.5.1` and creates descriptive sessions:

```python
session = WaybackSession(
    user_agent=(
        "archive-magic-fetch/0.1.0 "
        "(+https://github.com/RobJacobson/archive-magic)"
    )
)
```

Discovery uses one `WaybackClient` context for the CDX search. Concurrent
memento workers each open their own `WaybackSession`/`WaybackClient` via a
shared factory so in-flight downloads do not share one `requests.Session`.
Unspecified rate limits use the library defaults, which are process-wide shared
`RateLimit` objects (thread-safe): **0.4/s for CDX** and **8/s for mementos**
(one start every 125ms), matching Internet Archive guidance.

Fetch adds one job-wide adaptive admission gate for memento playback:

1. It distinguishes **request rate** (the library's shared 8/s limiter) from
   **in-flight concurrency** (this gate, initially two and capped by
   `--concurrency`).
2. HTTP 429 honors `Retry-After`, or 60 seconds when absent. Transient
   connection and timeout failures use exponential backoff from 1 to 30
   seconds.
3. Workers from the same failure wave share one backoff deadline. They do not
   independently multiply sleeps.
4. Each new failure wave halves the in-flight limit (minimum one). Every eight
   successful requests adds one slot until the configured ceiling is restored.
5. HTTP 429 does not consume a capture attempt: one worker announces the
   coordinated pause, every worker waits at the shared gate, and the same
   capture remains pending until Internet Archive accepts it or the user
   interrupts the job. Other transient failures receive at most six gate-level
   attempts. An exhausted `WaybackRetryError` follows normal playback-failure
   skip policy. Three consecutive failures at the same structured
   `IncompleteRead` byte boundary stop early as a persistent truncated
   response; changing boundaries retain the full retry budget.
6. Memento worker sessions use one immediate in-session retry, then surface
   the problem to the shared controller. A failed worker session resets its
   connection adapters before trying again.
7. A Requests `ContentDecodingError` is **not** a capacity signal. It does not
   advance the gate generation, reduce concurrency, consume a gate-level
   attempt, or sleep.
8. On the first decoding mismatch for a capture, that worker temporarily
   changes its private session from `Accept-Encoding: gzip, deflate` to
   `Accept-Encoding: identity`, resets its connection adapters, and retries.
   The previous header is restored when retrieval succeeds or exits.
9. A second mismatch under identity encoding raises
   `MalformedContentEncodingError`, warns, counts as a playback failure, and
   skips immediately. Fetch never guesses whether contradictory bytes should
   be decompressed or silently strips a replay header.

Discovery keeps its own one-retry policy for the CDX search path.

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

### 5.1 WARC paths

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

### 5.2 Loose website paths

When `--files` is `latest` or `all`, Fetch writes decoded semantic bodies under
`website/`. Paths follow the original site URL (Ruby wayback-machine-downloader
spirit), with an explicit host segment so multi-host collections stay distinct:

- host comes first (`www.` stripped; non-default ports use `--port-<n>`)
- directory URLs and extension-less directory-like paths become `.../index.html`
- **query strings are folded away** for loose-file paths only (WARC path/query
  encoding is unchanged): `main_style.css?v=1` → `main_style.css`
- `--files latest` writes `website/<host>/<site-path>` with no timestamp segment
- `--files all` writes `website/<host>/<14-digit-timestamp>/<site-path>`

**Newest-wins collisions:** After preferred paths are computed (including query
folding), if two or more selected captures map to the same
filesystem-equivalent website path, Fetch keeps exactly the capture with the
newest aware UTC timestamp and drops the others from the files plan. This is
the primary fix for `/` vs `/index.html` both mapping to `index.html`. Digest
suffixes are retained only for true same-path/same-time leftovers with distinct
digests; identical-digest leftovers keep the lexicographically smaller
`(urlkey, original, digest)` tuple.

Examples:

```text
/                 -> website/example.com/index.html
/index.html       -> website/example.com/index.html  (collides; newer wins)
/about            -> website/example.com/about/index.html
/a/b/             -> website/example.com/a/b/index.html
/css/style.css    -> website/example.com/css/style.css
/files/main_style.css?1546028705 -> website/example.com/files/main_style.css
/files/main_style.css?1719345030 -> website/example.com/files/main_style.css
                                 (collides; newer wins; no %3F in filename)

# --files all
/                 -> website/example.com/20060715085250/index.html
/css/style.css    -> website/example.com/20060715085250/css/style.css

# multi-host collection (*.example.com)
https://a.example.com/ -> website/a.example.com/index.html
https://b.example.com/ -> website/b.example.com/index.html
```

Path components use the same safety encoding and 240-byte component mindset as
WARC paths. Planned file-vs-directory conflicts reshape a file into
`existing-file/index.html` when that does not clobber another planned final
path. Existing final loose-file targets remain fatal (no resume). Empty or
failed playback bodies do not leave empty files.

### 5.3 Optional `--rewrite-local`

When `--rewrite-local` is set and at least one loose file was written, Fetch
rewrites text files under `website/` with extensions `.html`, `.htm`, `.css`,
and `.js` (case-insensitive) after writing completes. Decode failures are
skipped with a warning.

Rewrite rules (minimum viable):

- Root-relative `/path`, scheme-relative `//host/path`, and absolute
  `http(s)://host/path` references are rewritten to relative links when the
  host normalizes to a host segment present under `website/` and the local
  target exists (query folded when resolving; directories prefer `index.html`;
  `/` resolves to host-root `index.html` when present).
- `url(...)` is rewritten in `.css` and HTML inline styles only (not `.js`, so
  JS helpers named `url` are left alone). Straightforward `srcset` and
  `href`/`src`/`action` attributes are rewritten in HTML and JS.
- Under `--files all`, root-relative links resolve inside the file's timestamp
  directory; under `--files latest`, they resolve at the host root even when a
  site path segment looks like a 14-digit timestamp.
- Off-site hosts, `mailto:` / `tel:` / `javascript:` / `data:`, and missing
  local targets are left unchanged.
- Already-relative references (no scheme, not root- or scheme-relative) are
  left unchanged so a second pass does not corrupt links.

Limitations: this is best-effort for `file://` or a local static server; it does
not download missing link targets, rewrite third-party CDNs, or guarantee
perfect offline fidelity for JS-driven sites. Default CLI behavior without the
flag does not rewrite file contents.

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
`replay/index.cdxj` when `--warc` is enabled, and every planned `website/`
target when `--files` is enabled. Existing regular WARC and replay-index files
are resumable. Other existing final entries, broken final symlinks,
non-directory ancestors, component/path-limit violations, and other
uninspectable targets are fatal. Valid directory-symlink ancestors remain
supported. Existing loose website-file targets remain fatal.

WARC parsing validates an existing bucket before network work begins. An
empty, malformed, truncated, or non-WARC file is never appended to
automatically.

When `--warc none`, Fetch skips WARC allocation, WARC/replay preflight, WARC
export, and replay indexing entirely.

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

Retrieval is a **resume scan / global worker-pool fetch / ordered-write**
pipeline:

1. Parse every existing planned WARC and map committed response/revisit records
   back to the current selected CDX captures.
2. Plan captures that need a network fetch. Already committed captures are
   omitted. Later captures that share a CDX
   digest/status with an earlier eligible capture are omitted from the plan
   (they become revisits after a successful earlier write).
3. Flatten planned work across sorted URL-key groups. A bounded lookahead of
   twice the worker ceiling can therefore include captures from several URLs;
   no URL's fetches wait for the preceding URL's writes.
4. One `ThreadPoolExecutor` and its per-thread Wayback sessions live for the
   complete WARC stage, preserving HTTP connection pools across URL groups.
5. Each worker prints `Fetched` as soon as a network body is completely
   buffered, then immediately takes another admitted job.
6. On the main thread, walk captures in deterministic URL/date order. Before
   each write, wait only for that capture's fetch future (if any), then write
   the WARC or loose file and print `Wrote`.

`--concurrency 1` is true serial on-demand fetch during the write loop (no
worker pool), matching the original diagnostic UX.

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

Wayback occasionally returns a replay response that declares
`Content-Encoding: gzip` while sending bytes that cannot be decoded as gzip.
The raw response may be an already-decoded body or only a clipped decoded
prefix bounded by stale compressed-representation metadata, so stripping the
header is not a safe recovery. Requests raises `ContentDecodingError` before
Fetch receives trustworthy semantic bytes. The identity-encoding retry
described in section 3 handles recoverable cases without slowing unrelated
workers or changing the normal compressed transfer path; a persistent
mismatch warns and skips.

Repeated incomplete transfers normally follow the adaptive connection retry
policy. When three consecutive attempts stop at the same received/expected
byte boundary, Fetch treats the outcome as a persistent truncated Wayback
response, skips immediately, and reports the structured byte counts. It never
writes a partial response as a successful capture.

Known CDX 3xx rows are counted and omitted before playback. A statusless row
that plays back as 3xx is also counted and omitted. A known CDX/Memento status
mismatch warns and skips the capture. Fetch does not synthesize redirects,
unavailable-resource metadata, or broken-resource responses.

Approved Wayback playback/availability failures—including a content-encoding
mismatch that persists under identity encoding—warn, count as playback
failures, and allow later captures to continue. Explicit rate limits pause and
retry the same operation during both discovery and playback; they are never
reported as skipped captures. Unexpected formats, local filesystem errors, and
serialization failures are fatal.

Fetch completion order is unrestricted across both dates and URLs, so
`Fetched` lines intentionally appear in completion order. Within one command,
`Wrote` lines and new WARC records follow the sorted URL/date commit cursor.
On a resume, missing records are appended after all previously committed
records, so the complete physical WARC need not remain chronological. The
replay index restores sorted lookup order. The bounded window prevents the
executor queue and WARC-only completed bodies from growing with the entire
discovery result. A result is released after its WARC write unless the
loose-file plan also needs it; shared captures stay cached only until that
second consumer writes them.

## 9. Shared-WARC export and deduplication

One bucket owns one lazily opened WARC stream:

1. Validate and scan an existing WARC, if present.
2. Iterate its URL-key groups in sorted order.
3. Draw only missing, already-planned results from the global bounded fetch
   window.
4. Initialize fresh source and semantic maps for the current group, seeding
   them as committed captures are encountered.
5. Process captures in timestamp order on the main thread, waiting on each
   needed fetch just before writing.
6. Lazily open an existing WARC in append mode only when a missing record must
   be committed, or exclusively create a new WARC.
7. Reuse the bucket writer for later groups and close it after every assigned
   group completes.

A new WARC receives one `warcinfo` record. A resumed WARC does not receive a
second one. If every assigned capture is omitted or already committed, the
stream is never opened and its bytes remain unchanged.

Deduplication never crosses a URL-key group boundary, even when two groups
share a WARC. Within a group:

- a successful CDX digest/status source signature can avoid later playback;
- statusless CDX rows can reuse a successful matching digest;
- semantic payload digest plus actual HTTP status selects response versus
  revisit; and
- maps are updated only after successful validation and serialization.

Every new response or revisit carries
`WARC-Archive-Magic-Capture-ID`, a stable SHA-256 identity derived from the
selected CDX record. On reruns this permits exact record-level matching without
network access. WARCs produced before this header was introduced are matched
conservatively by capture timestamp, target/source URI, and HTTP status.
Ambiguous legacy records are not guessed: their captures are fetched again.

`export_all()` returns:

```text
ExportResult(summary, created_warcs)
```

It does not print the aggregate summary. For API compatibility,
`created_warcs` contains every validated planned WARC available after the
command, including unchanged resumed files, and is the complete input to replay
indexing.

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

Only WARCs in the current deterministic export plan are indexed, including
resumed files. Other files beneath `archives/` are never discovered
recursively.

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

The index is written to a temporary sibling in `replay/`, then atomically
replaces the previous index. A failed rebuild leaves the previous valid index
untouched. This is required because appending missed WARC records adds replay
entries.

An indexing or publication failure may leave completed WARCs, but never a
truncated final index. No WARC files means no replay directory or empty index.

## 11. Console and failure policy

Ordinary output remains compact:

```text
Job started: 2026-07-24T19:04:12Z
Starting https://example.com/images/logo.png
Rate limited by Internet Archive during playback; pausing all downloads for 60s before retrying...
Fetched 20180709183022 https://example.com/images/logo.png
Fetched 20170604120533 https://example.com/images/logo.png
Wrote 20170604120533 [a19f7c2e]
Wrote 20180709183022 [c46a801d]
WARNING skipped 20190812143015 https://example.com/images/logo.png: invalid Wayback replay response: Content-Encoding declares gzip, but the body could not be decoded; retrying with Accept-Encoding: identity also failed
Skipping https://example.com/styles/site.css (already captured)
Summary: 235 selected for warc (all); 4 responses; 1 revisits; 221 already present; 9 redirects omitted; 2 playback failures (1 invalid content encoding, 1 truncated response)
Files: 180 written (latest); 2 playback failures (1 invalid content encoding, 1 other); 0 redirects omitted
Job ended: 2026-07-24T19:18:24Z
Job duration: 14.2 minutes
```

`Fetched` means a new network response body has landed in the bounded result
buffer. Cache hits and source-signature revisits do not print another
`Fetched`. `Wrote` means the ordered writer has committed that capture to its
WARC or loose-file destination. When every eligible capture for a URL group
was recovered from committed WARC records, one
`Skipping ... (already captured)` line replaces the per-capture output and the
group does not enter the retrieval window. Existing captures in a partially
complete group are silent while its missing captures are processed normally.

Every parsed CLI job prints UTC start and end times in second-precision
ISO-8601 form. A monotonic clock supplies total elapsed time, reported in
decimal minutes with one digit after the decimal point. The end and duration
lines are emitted from the outer job boundary on success, no-op/empty results,
usage validation failures, and caught runtime failures. Argument-parser exits
such as `--help` occur before a job begins.

The WARC summary reports selected rows, newly written responses/revisits,
already-present captures, deliberately omitted redirects, and playback
failures for the active `--warc` mode. When
`--files` is enabled, a second line reports written bodies and failures for
that mode. Source-signature revisits that avoid the network remain silent.

The CLI catches fatal errors, prints `ERROR: ...` to stderr, and returns 1.
Source publication, WARC writing, and replay publication are individually
safe, but the whole collection is intentionally not one transaction.

## 12. Project responsibilities and dependencies

The flat package contains:

| File | Responsibility |
| --- | --- |
| `cli.py` | Arguments, job timing, `--concurrency`, output-mode defaults, client lifecycle, stage gating, final summary/error boundary |
| `discovery.py` | Complete search, rate-limit retry, duplicate collapse, URL-key grouping, latest/all selection |
| `paths.py` | Collection normalization, safe readable WARC/website paths, collision buckets, preflight |
| `publication.py` | Same-filesystem atomic no-replace file/directory publication |
| `provenance.py` | Source CDX and query-manifest serialization/publication |
| `retrieval.py` | Exact playback, adaptive transport backoff, identity-encoding recovery, bounded worker fetch, shared result ownership |
| `export.py` | Global fetch planning, per-group deduplication, ordered WARC commit |
| `files.py` | Ordered loose website-file writing under `website/` with wait-for-next |
| `rewrite_local.py` | Optional post-write HTML/CSS/JS local-link rewrite |
| `warc.py` | WARC dates, exclusive creation/resume append, response/revisit serialization |
| `replay.py` | Final-WARC CDXJ generation and atomic replacement |

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
- resumable regular WARC/index targets, rejected unsafe targets, broken
  symlinks, and invalid ancestors;
- complete source CDX/manifest content, checksums, suffix allocation,
  concurrent publication, and cleanup;
- shared-WARC ownership with one `warcinfo` and fresh per-group maps;
- record-level rerun skipping, append-only missing capture recovery, legacy
  matching, malformed-WARC rejection, and replay-index replacement;
- retrieval fields, redirect/status policy, adaptive transport retry,
  identity-encoding recovery/restoration, deduplication, and failure behavior;
- sorted response/revisit CDXJ semantics and nested filenames;
- exact offset/length selection of independently compressed WARC members;
- replay-index replacement and indexer failures;
- `--warc` / `--files` mode gating, latest selection preference, website path
  layouts (query folding, newest-wins), `--rewrite-local` validation/rewrite,
  and shared-capture fetch fan-out; and
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

- migration or rewriting of pre-existing archive records;
- automatic repair or truncation of malformed existing WARCs;
- concurrent writers targeting the same collection;
- output-root configuration;
- a generic archive-source interface or Common Crawl;
- source-WARC representation-byte preservation;
- redirect preservation or unavailable-capture metadata;
- cross-URL-key payload deduplication;
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
