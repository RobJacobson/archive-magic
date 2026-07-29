# Archive Magic Fetch Architecture

**Status:** Implemented collection architecture

**Scope:** `archive-magic-fetch` only

**Updated:** July 28, 2026

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
- `archive/` contains WARC 1.0 response records written by Fetch
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
- a thin CLI process boundary and one explicit fetch-job workflow;
- a flat `src/archive_magic_fetch/` package; and
- no source-adapter hierarchy, database, checkpoint store, output reuse, or
  publication service.

## 2. CLI contract and data flow

The public command is:

```text
archive-magic-fetch URL_PATTERN
  [--start DATE] [--end DATE]
  [--warc {none,latest,all}]
  [--files {none,latest,unique,all}]
  [--rewrite-local]
  [--concurrency N]
  [--retries N]
```

| Flag | Values | Default |
| --- | --- | --- |
| `--warc` | `none`, `latest`, `all` | `all` |
| `--files` | `none`, `latest`, `unique`, `all` | `none` |
| `--rewrite-local` | flag | off |
| `--concurrency` | integer ≥ 1 | `8` |
| `--retries` | integer ≥ 0 | `8` |

`--concurrency 1` restores serial URL-group workers for diagnostics. Values
above 8 mostly queue behind the Wayback client's independent 8 requests/second
memento pacing. The value directly sets the fixed worker-pool ceiling; Fetch
does not dynamically reduce or ramp concurrency.

`--retries N` controls application-owned retries after the initial Internet
Archive request. Retry `n` waits exactly `5 * (2^n)` seconds. Zero disables
retries;
values are otherwise uncapped. Exceptionally large waits are slept in bounded
chunks to avoid platform timeout overflow.

Argument parsing validates these numeric bounds and cross-option requirements
before a job begins. Successful parsing produces one immutable `FetchRequest`;
the fetch workflow does not receive or depend on an `argparse.Namespace`.

The two axes are independent. Default behavior remains full WARC history plus
replay CDXJ with no loose files. `--warc none --files none` exits successfully
with `Nothing to do: both --warc and --files are none` and performs no
discovery. `--rewrite-local` requires `--files latest`, `unique`, or `all`
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

The successful process and job flow is:

```text
parse and validate arguments, apply date defaults, and build FetchRequest
    -> start the timed, console-mirrored process boundary
    -> run the fetch-job workflow
    -> if warc=none and files=none: message + exit 0
    -> derive and validate the collection name
    -> create one application-owned WaybackSession and WaybackClient
    -> fully materialize discovery
    -> if empty, print "No captures found" and exit successfully
    -> atomically publish the source acquisition (full discovery set)
    -> group records by urlkey
    -> build warc_selection and files_selection from output modes
    -> if files enabled: plan website/ paths (MIME + query folding + newest-wins)
    -> export enabled outputs with one reusable Wayback client per worker thread
    -> if warc enabled: generate replay/index.cdxj
    -> if --rewrite-local and at least one file was written: rewrite under website/
    -> print aggregate summary
    -> close the client/session
    -> map job success/failure to exit status and print end time/duration
```

Selection is an export transform. Provenance always stores the full CDX result
for the query window, not the post-`latest` subset.

`latest` keeps exactly one capture per urlkey group:

1. newest capture whose CDX status is `200`; else
2. newest capture whose status is present and not `3xx`; else
3. newest capture whose status is `3xx`; else
4. omit the statusless-only group.

WARC and loose-file selection remain independent, but one URL-group worker
writes both outputs. A capture selected by both axes is retrieved once by
object identity; distinct value-equal CDX rows remain distinct.

The aggregate summary is printed only after enabled output stages succeed. A
fatal indexing or replay-publication error therefore cannot follow an apparently
successful final summary. If every WARC capture is omitted or skipped, no WARC
or replay index is created and the successful aggregate summary is still printed.

## 3. Wayback client and discovery

Fetch pins `wayback==0.5.1` and creates descriptive sessions:

```python
session = ArchiveMagicWaybackSession(
    user_agent=(
        "archive-magic-fetch/0.1.0 "
        "(+https://github.com/RobJacobson/archive-magic)"
    )
)
```

Discovery uses one `WaybackClient` context for the CDX search. Concurrent
memento executor threads lazily open one `WaybackSession`/`WaybackClient` and
reuse it across successive WARC jobs. In-flight threads never share one
`requests.Session`, and all worker clients close after executor shutdown.
The application session sets `wayback`'s retry count to zero.
Unspecified rate limits use the library defaults, which are process-wide shared
`RateLimit` objects (thread-safe): **0.4/s for CDX** and **8/s for mementos**
(one start every 125ms), matching Internet Archive guidance.

Fetch keeps playback scheduling deliberately small:

1. The fixed worker pool runs at most `--concurrency` WARC-bucket or standalone
   file-group tasks. One task exclusively owns its WARC, processes assigned URL
   groups serially, and uses one client. The library's independent shared
   limiter continues to cap **request rate** at 8 starts per second.
2. Archive Magic owns one retry loop per capture. Except for incomplete
   responses, retry `n` waits `5 * (2^n)` seconds, or the larger server
   `Retry-After` value. A sleeping worker does not pause or change another
   worker.
3. Transient connection/reset/timeout failures and non-Memento Internet
   Archive statuses 413, 421, 429, 500, 502, 503, 504, and 599 retry. A
   response with `Memento-Datetime` remains a genuine historical capture even
   when its archived status is 5xx.
4. Exhaustion after `--retries N` means `N+1` total attempts. The capture is
   skipped, later work continues, and the completed command reports every
   failed capture URL before exiting nonzero.
5. An incomplete response retries immediately. Two consecutive failures at
   the same structured `IncompleteRead` byte boundary stop early as a
   persistent truncated response; changing boundaries retain the full retry
   budget.
6. The `wayback` library performs no retries; its process-wide baseline CDX
   and Memento request-start limiters remain enabled.
7. Playback response bodies are streamed so the Memento context owns response
   closure and connection-pool cleanup.
8. A Requests `ContentDecodingError` is a playback representation problem, not
   a rate-limit or transport-capacity signal. Fetch makes at most one
   additional exact request for undecoded replay bytes. This is a bounded
   integrity-recovery request, not a transport retry: it has no backoff and
   never enters the application retry loop.
9. Raw recovery succeeds only when the second response is a Memento with the
   expected HTTP status and its complete raw body exactly matches the selected
   capture's valid CDX SHA-1 payload digest. Fetch then removes contradictory
   representation headers and records the recovery in console output.
10. A missing or malformed CDX digest, non-Memento response, status mismatch,
    incomplete transfer, request error, or digest mismatch raises
    `MalformedContentEncodingError`, warns, counts as a playback failure, and
    skips the capture without further requests or connection-pool resets.

Discovery uses the same `--retries` count and deterministic pacing. A
retryable failure discards partial rows and restarts the complete search.
Discovery exhaustion is fatal because no trustworthy capture set exists.

Discovery calls:

```python
client.search(
    url_pattern,
    from_date=date_start,
    to_date=date_end,
    limit=10_000,
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

The collection boundary is validated up front. Individual WARC targets are
created lazily, so the filesystem reports any target-specific path or ancestor
failure when the bucket is first written.

### 5.2 Loose website paths

When `--files` is enabled, Fetch writes decoded semantic bodies under
`website/`. Paths follow the original site URL (Ruby wayback-machine-downloader
spirit), with an explicit host segment so multi-host collections stay distinct:

- host comes first (`www.` stripped; non-default ports use `--port-<n>`)
- explicit URL filename extensions are preserved
- extensionless HTML/XHTML becomes `.../index.html`
- known extensionless non-HTML content uses a conventional terminal suffix;
  for example `/download/report/` with `application/pdf` becomes
  `download/report.pdf`
- unknown MIME remains extensionless and is never guessed to be HTML
- **query strings are folded away** for loose-file paths only (WARC path/query
  encoding is unchanged): `main_style.css?v=1` → `main_style.css`
- `--files latest` writes `website/<host>/<site-path>` with no timestamp segment
- `--files unique` writes one timestamped file per full response and skips
  captures represented as WARC revisits
- `--files all` writes `website/<host>/<14-digit-timestamp>/<site-path>`

**Newest-wins collisions:** After preferred paths are computed (including query
folding), if two or more selected captures map to the same
filesystem-equivalent website path, Fetch keeps exactly the capture with the
newest aware UTC timestamp and drops the others from the files plan. This is
the primary fix for `/` vs `/index.html` both mapping to `index.html`. Digest
suffixes are retained only for true same-path/same-time leftovers with distinct
digests. An identical-digest tie is rejected as an ambiguous output path
instead of silently dropping a selected capture.

Examples:

```text
/                 -> website/example.com/index.html
/index.html       -> website/example.com/index.html  (collides; newer wins)
/about            -> website/example.com/about/index.html
/download/report/ (PDF) -> website/example.com/download/report.pdf
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

CDX MIME plans the destination. The retrieved response `Content-Type` is
normalized without parameters and must produce the same destination. A
mismatch skips and reports only the loose-file write; WARC export continues.

### 5.3 Optional `--rewrite-local`

When `--rewrite-local` is set and at least one loose file was written, Fetch
rewrites text files under `website/` with extensions `.html`, `.htm`, `.css`,
and `.js` (case-insensitive) after writing completes. Decode failures are
skipped with a warning.

Rewrite rules (minimum viable):

- Root-relative `/path`, scheme-relative `//host/path`, and absolute
  `http(s)://host/path` references are rewritten to relative links when the
  host normalizes to a host segment present under `website/` and the local
  target exists. Resolution is deliberately filesystem-driven: it recognizes
  conventional HTML directory indexes and explicit filenames, but does not
  guess MIME-derived suffixes. For example, `/download/report/` remains
  unchanged when the only local target is `download/report.pdf`.
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

## 6. Direct WARC bucket allocation

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

Allocation is a dictionary pass. Each URL key constructs one preferred path.
For file/directory conflicts, each normalized key walks its strict component
prefixes in a set and assigns itself to the shortest planned prefix. Work is
proportional to total path depth; there is no pairwise bucket or filesystem
ancestor scan.

The lexically smallest safe candidate supplies the displayed bucket spelling.
Buckets sort by collection-relative path; URL keys within a bucket sort
lexically. Allocation is independent of discovery order.

Collision buckets retain resource identity through WARC metadata and distinct
CDXJ offsets. Sharing storage avoids arbitrary filename suffixes and prevents
rare naming collisions from aborting unattended exports.

Each WARC owns one lazily created `.tmp` opened with exclusive creation.
Existing final WARCs may supply exact cached responses. Completed temporary
WARCs are validated and atomically replace the readable final path. A stale
`.tmp` or invalid ancestor fails only when that bucket actually needs a writer.

When `--warc none`, Fetch skips WARC allocation, WARC export, and replay
indexing entirely.

## 7. Source provenance

After complete successful discovery and before output selection, Fetch
publishes:

```text
sources/<acquisition>/
├── captures.cdx.gz
├── query.json
└── log.txt
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

Rows are streamed directly into gzip while being counted. The completed gzip
is hashed for the manifest; no uncompressed or compressed whole-file byte
buffer is built. The gzip header has no temporary filename and uses the
acquisition time as its explicit `mtime`.

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

The source CDX and query manifest are written in a temporary sibling directory
under `sources/`. Publication uses a same-filesystem atomic no-replace
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

After publication, `log.txt` receives the complete job console transcript.
Output produced before the acquisition directory exists is buffered, then
flushed into the log; later stdout and stderr writes are mirrored immediately.

## 8. Retrieval and WARC construction

Retrieval and writing happen inside exclusive WARC-bucket tasks:

1. The executor assigns each bucket to one worker thread.
2. That task processes its URL groups serially and privately owns its WARC.
3. The thread reuses its Wayback client and connection pool on later tasks.
4. Each URL group keeps a private digest-to-representative dictionary.
5. The completed URL group's buffered console block is emitted atomically.

`--concurrency 1` performs serial on-demand retrieval. Work and created-WARC
results otherwise remain in completion order.

Fetch requests each exact original-mode Memento:

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
Fetch receives trustworthy semantic bytes.

Fetch handles this specific contradiction with one conservative, format-free
recovery:

1. Close the failed streamed Memento response.
2. Require a syntactically valid SHA-1 digest from the selected CDX row.
3. Request the same exact original-mode replay once more, without following
   redirects or asking Requests to decode its content representation.
4. Require the raw response to remain a Memento with the same historical HTTP
   status.
5. Compute SHA-1 over the raw body and require an exact CDX digest match.
6. Treat a matching body as the semantic archived payload, remove stale
   content/transfer encoding, length, range, ETag, and representation-digest
   headers, and write a newly computed semantic `Content-Length`.
7. Report `recovered invalid content encoding via CDX digest` before reporting
   the enabled WARC/file writes.

The digest match is the recovery boundary. It proves that the accepted bytes
are the payload identified by the archive index; it does not prove that the
original crawler captured a syntactically complete document. Fetch therefore
does not inspect URL extensions, MIME signatures, closing HTML tags, or other
format-specific completeness signals.

Fetch cannot safely recover a raw body that differs from the CDX digest.
Length agreement is insufficient because a decoded body can be clipped at a
stale compressed `Content-Length`. `Accept-Encoding: identity`, repeated
playback, recognizable HTML/PDF prefixes, and blindly removing
`Content-Encoding` do not establish fidelity. Missing/invalid CDX digests,
raw-request errors, incomplete raw transfers, non-Memento responses, status
mismatches, and digest mismatches remain categorized playback failures. Fetch
does not attempt source ARC/WARC extraction or substitute a nearby capture.

Repeated incomplete transfers normally follow the bounded connection retry
policy. When two consecutive attempts stop at the same received/expected byte
boundary, Fetch treats the outcome as a persistent truncated Wayback response,
skips immediately, and reports the structured byte counts. This permits one
immediate retry for a transient incomplete transfer without repeatedly
requesting a deterministically truncated archive record. The retry is reported
as `retrying after incomplete response` and does not use exponential backoff.
Range stitching is not attempted because a stable boundary can represent
missing source bytes rather than a resumable transport interruption. Fetch
never writes a partial response as a successful capture.

Selected 3xx rows are played back exactly without following their redirects
and are stored as full WARC responses. Redirect bodies remain omitted from
loose-file output. A known CDX/Memento status mismatch warns and skips the
capture. Fetch does not synthesize redirects, unavailable-resource metadata,
or broken-resource responses.

Approved Wayback playback/availability failures—including a content-encoding
mismatch—warn, count as playback failures, and allow later captures to
continue. Explicit rate limits pause and retry only the operation that
encountered them during discovery or playback. Unexpected formats, local
filesystem errors, and serialization failures are fatal.

An existing final WARC is an untrusted local response cache. A structural
inventory failure warns once for the bucket and disables that cache; Fetch
continues through Wayback. An invalid exact cached response warns for its
capture and also falls back to Wayback without counting the cache problem as a
playback failure.

Workers may finish in any order. WARC records remain timestamp-ordered within
each URL group, while completed console blocks are emitted atomically in
completion order.

## 9. WARC export

One worker task owns one collision-bucket rebuild:

1. Inventory full `response` records in an existing final WARC by exact target
   URI, WARC date, and source replay URI. Store compressed member offsets and
   lengths, not bodies; duplicate or incomplete identities are unusable.
2. Iterate assigned URL-key groups in sorted order and process each group's
   captures in timestamp order.
3. Keep one private dictionary of valid CDX SHA-1 digests for the current URL
   group only.
4. If the digest already has a representative in the new WARC, write an
   identical-payload-digest `revisit` without consulting the old WARC or
   Wayback.
5. Otherwise, try the exact old full response. Validate its WARC payload digest
   and known status while loading its independently compressed member.
6. If the exact response is absent, ambiguous, invalid, or only a revisit,
   fetch and validate it from Wayback. Fetch does not search the old WARC for a
   different record with the same digest.
7. Lazily create `<final>.tmp`, using the final filename in `warcinfo`, and
   write the locally reused or downloaded representative as a full `response`.
8. Materialize enabled loose files from the same body: `unique` writes
   representatives only, while `all` writes every timestamp.
9. Release group state before the next URL group. After the bucket finishes,
   close and fully validate the temporary WARC, then atomically replace the
   final WARC with `os.replace()`.

Digest identity is scoped to one URL key, so matching bodies at different URLs
each retain a full response. Missing or malformed CDX digests are fetched and
written independently. Normally decoded playback bodies are not compared
byte-for-byte with CDX digests because Wayback may legitimately transform a
stored content representation. The comparison is mandatory only for the raw
fallback after automatic content decoding has already failed.

Every response and revisit uses the CDX `capture.original` value as
`WARC-Target-URI`. Existing percent escapes such as `%7B` remain unchanged;
the playback URL is retained separately as `WARC-Source-URI`.

Every rebuilt WARC receives one `warcinfo` record. A normal run publishes a
structurally valid partial WARC after individual playback failures so a later
run can reuse its successes and retry its gaps. If every assigned capture is
omitted or fails, Fetch removes its temporary and preserves an existing final
WARC, or creates no final WARC when none existed. Caught fatal errors clean the
owned temporary and preserve the final; abrupt termination can leave the
exclusive `.tmp` marker. Fetch never modifies or appends to a final WARC in
place.

`export_all()` returns:

```text
ExportResult(summary, final_warcs, files_summary, failed_capture_urls)
```

`final_warcs` contains every available WARC in deterministic export-plan order,
including atomically replaced and preserved finals, and is the complete input
to replay indexing. `failed_capture_urls` contains each failed clickable
Wayback URL once for final reporting and the partial-failure exit status.

## 10. Replay CDXJ

Fetch pins `cdxj-indexer==1.4.6` and invokes its Python API with:

```python
CDXJIndexer(
    output=temporary_index,
    inputs=final_warcs,
    sort=True,
    records="response,revisit",
    dir_root=collection_root,
).process_all()
```

Every available WARC in the current deterministic export plan is indexed.
Other files beneath `archives/` are never discovered recursively. An existing
regular replay index is accepted and atomically replaced; a failed rebuild
leaves the previous index intact.

The resulting `replay/index.cdxj`:

- is sorted by replay URL key and timestamp;
- derives each replay URL key from the WARC record's `WARC-Target-URI`;
- includes collection-relative filenames such as
  `archive/posts/index.warc.gz`;
- records true compressed offsets and member lengths; and
- indexes both response and revisit records.

The Internet Archive source `urlkey` is not copied into replay entries.

Response entries contain target URL, response MIME, HTTP status, payload
digest, filename, offset, and length.

The index is written to a temporary sibling in `replay/`, then atomically
published.

An indexing or publication failure may leave completed WARCs, but never a
truncated final index. No WARC files means no replay directory or empty index.

## 11. Console and failure policy

Ordinary output remains compact:

```text
Job started: 2026-07-24T19:04:12Z
https://web.archive.org/web/20190812143015/https://example.com/images/logo.png : retry 1/8 in 10s after Internet Archive returned retryable HTTP 503
[completed 1/235] example.com/images/logo.png
https://web.archive.org/web/20170604120533/https://example.com/images/logo.png : wrote response
https://web.archive.org/web/20180709183022/https://example.com/images/logo.png : wrote revisit
https://web.archive.org/web/20190110102030/https://example.com/about : recovered invalid content encoding via CDX digest
https://web.archive.org/web/20190110102030/https://example.com/about : wrote response
https://web.archive.org/web/20190812143015/https://example.com/images/logo.png : failed during playback
  WARNING: original Wayback replay could not be decoded by the HTTP client (Content-Encoding: gzip): incorrect header check; raw recovery was not verified by the CDX digest, so the capture was discarded
Summary: warc 1 response, 1 revisit, 1 failed
Summary: 235 selected for warc (all); 189 responses; 44 revisits; 0 redirects omitted; 2 playback failures (1 invalid content encoding, 1 truncated response)
Files: 180 written (latest); 2 playback failures (1 invalid content encoding, 1 other); 0 redirects omitted
Failed captures:
https://web.archive.org/web/20190812143015/https://example.com/images/logo.png
Job ended: 2026-07-24T19:18:24Z
Job duration: 14.2 minutes
```

Each completed URL group is buffered and printed with one locked console write.
Blocks appear in completion order without line interleaving. Their headers use
readable original URLs rather than CDX SURT keys, and capture lines begin with
clickable Wayback view URLs.

Every successfully parsed CLI job prints UTC start and end times in
second-precision
ISO-8601 form. A monotonic clock supplies total elapsed time, reported in
decimal minutes with one digit after the decimal point. The end and duration
lines are emitted from the outer job boundary on success, no-op/empty results,
partial failures, and caught runtime failures. Argument-parser exits, including
`--help` and usage-validation failures, occur before a job begins and therefore
do not print job timing.

The WARC summary reports selected rows, written responses, revisits, and
playback failures for the active `--warc` mode. The legacy redirect-omission
count remains zero for WARC output. When `--files` is enabled, a second line
reports written bodies, omitted redirects, and failures for that mode.

The CLI catches fatal errors, prints `ERROR: ...` to stderr, and returns 1.
It also returns 1 after finalizing otherwise usable outputs when selected
captures failed, listing each failed clickable Wayback URL once.
Source publication, WARC writing, and replay publication are individually
safe, but the whole collection is intentionally not one transaction.

## 12. Project responsibilities and dependencies

The flat package contains:

| File | Responsibility |
| --- | --- |
| `cli.py` | Argument validation/defaults, job timing, console mirroring, exception and exit-status handling |
| `job.py` | Immutable fetch request, discovery/export orchestration, output finalization and reporting |
| `discovery.py` | Complete search, application retry, URL-key grouping, output selection |
| `paths.py` | Collection normalization, safe readable WARC/website paths, collision buckets, preflight |
| `publication.py` | Same-filesystem atomic no-replace file/directory publication |
| `provenance.py` | Source CDX and query-manifest serialization/publication |
| `retry.py` | Retry classification, deterministic delays, structured transient IA responses |
| `retrieval.py` | Exact playback, application retry, CDX-gated raw recovery |
| `export.py` | Concurrent bucket ownership, response/revisit/file writing, thread clients |
| `files.py` | Loose website-file writing under `website/` |
| `rewrite_local.py` | Optional post-write HTML/CSS/JS local-link rewrite |
| `warc.py` | WARC dates, exact-response cache, validation, serialization |
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

The deterministic suite separates CLI-boundary, fetch-job, and discovery
coverage. It uses real `CdxRecord` values, fake Wayback clients, temporary
collections, actual `warcio` parsing, and the real pinned CDXJ indexer. It does
not contact Internet Archive.

Acceptance covers:

- collection normalization, built-in IDNA, ports, and wildcard scopes;
- safe readable paths, query retention, component truncation, and collision
  allocation;
- independent `NAME_MAX` and `PATH_MAX` checks and fallbacks;
- accepted existing regular WARC/replay targets; rejected `.tmp`, unsafe
  targets, broken symlinks, and invalid ancestors;
- complete source CDX/manifest content, checksums, suffix allocation,
  concurrent publication, and cleanup;
- shared-WARC ownership with one final-named `warcinfo`, cache-first rebuilding,
  exact response reuse, revisit fallback, partial backfill, temporary cleanup,
  validation, and atomic replacement;
- retrieval fields, redirect/status policy, per-operation exponential retry,
  independent worker backoff, thread-client reuse, CDX-digest-gated raw
  content-encoding recovery, and final failed-URL behavior;
- complete-plan sorted response CDXJ semantics and nested filenames;
- exact offset/length selection of independently compressed WARC members;
- replay-index replacement and indexer failures;
- `--warc` / `--files` mode gating, latest selection preference, website path
  layouts (query folding, newest-wins), `--rewrite-local` validation/rewrite,
  and independent output stages; and
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
- appending to or reusing existing outputs;
- concurrent writers targeting the same collection;
- output-root configuration;
- a generic archive-source interface or Common Crawl;
- source-WARC representation-byte preservation;
- redirect preservation or unavailable-capture metadata;
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
