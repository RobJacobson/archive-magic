# Archive Magic Fetch architecture

Archive Magic Fetch performs two concrete operations:

1. Search Internet Archive for each URL's captures across time.
2. Build final WARC files from those URL histories.

The implementation deliberately separates searching from writing. WARC builds
start from the primary search selection. Permanent redirects discovered while
writing a response enqueue additional URL histories as new WARC work.

## Command line

```text
archive-magic-fetch URL_PATTERN
  [--start DATE]
  [--end DATE]
  [--warc none|latest|all]
  [--files none|latest|unique|all]
  [--redirect-capture none|page|website]
  [--workers N]
  [--retries N]
  [--rewrite-local]
```

`URL_PATTERN` seed scope:

| Pattern | CDX meaning |
|---|---|
| `*.example.com` | Domain match: apex host plus all subdomains (preferred form) |
| `*.example.com/*` | Same as `*.example.com` |
| `example.com/*` | Path prefix on that single host |
| `example.com` | Exact URL match for that page |

Domain wildcard is orthogonal to `--redirect-capture`, which only controls
expansion of permanent redirect Locations after the seed search.

`--workers` defaults to 8. It is the maximum number of simultaneous WARC
builds. There is no `--concurrency` alias.

CDX searches remain serial because they define the set of work. Independent
WARC files use a bounded thread pool. Each pool thread lazily creates and
reuses one Wayback client; all clients retain the shared process-wide
Internet Archive rate limit.

## Collection paths

The collection directory is derived from the originally requested domain.
Each captured domain then receives one direct folder under `archive/`:

```text
<collection>/
├── archive/
│   ├── example.com/
│   │   ├── index.warc.gz
│   │   └── images/
│   │       └── logo.png.warc.gz
│   └── target.org/
│       └── documents/
│           └── report.pdf.warc.gz
├── replay/
│   └── index.cdxj
├── sources/
│   └── <search timestamp>/
│       ├── captures.cdx.gz
│       ├── query.json
│       └── log.txt
└── website/                 # only when --files is enabled
```

Domain folders use these rules:

- lowercase DNS names;
- remove a trailing DNS dot;
- convert internationalized DNS names to ASCII IDNA;
- remove one exact leading `www.`;
- omit HTTP port 80 and HTTPS port 443;
- preserve every other explicit port in the authority;
- bracket normalized IPv6 addresses before encoding;
- encode the authority with the ordinary filesystem-safe percent encoder.

Examples:

```text
https://example.com/          -> archive/example.com/index.warc.gz
https://example.com/about     -> archive/example.com/about.warc.gz
https://example.com:8443/     -> archive/example.com%3A8443/index.warc.gz
http://example.com:8080/a     -> archive/example.com%3A8080/a.warc.gz
https://münich.example/       -> archive/xn--mnich-kva.example/index.warc.gz
```

Domains never share WARC files merely because their resource paths match.
Filesystem-equivalent path and file/directory collision handling is scoped to
one domain folder. Within that folder, multiple URL histories may share a WARC
when readable paths collide. Encoded query strings and the existing
file/directory collision rules remain unchanged.

## Search and selection

`search_captures()` materializes a complete CDX result within the requested
date bounds. Every nonempty search is saved under `sources/` before it is
used downstream.

`group_by_url()` groups records by:

```text
(normalized domain folder, CDX urlkey)
```

and orders each history by capture time. `select_captures()` applies the WARC
and loose-file modes independently:

- `none`: select nothing;
- `all`: retain every capture;
- `unique` for files: retain the full history so digest reuse can choose
  representative bodies;
- `latest`: prefer the newest 200, then the newest known non-redirect,
  then the newest known redirect.

Primary WARC and file selection happens before WARC construction. Redirect
expansion runs inline while those WARCs are written: only **new** URL
histories introduced by Location targets are enqueued, with their full CDX
histories, and they are never added to loose website-file output. Already
selected primary histories keep their original selection mode.

## Redirect discovery

Redirect discovery is inline with WARC construction when
`--redirect-capture` is `page` or `website`.

When a WARC worker successfully stores a 301 or 308 response, it reuses that
downloaded response to resolve an HTTP or HTTPS `Location` relative to the
captured URL (fragment removed). Missing or invalid `Location` values are
warnings. Statuses such as 302, 303, and 307 are still preserved in final
WARCs when selected, but never introduce searches.

The coordinator translates each unseen target into either:

- an exact page search for `--redirect-capture page`; or
- for `--redirect-capture website`, a normalized host search when the
  Location is a site root (`/` with no query), otherwise an exact page search.

Deep Location paths never trigger host-wide CDX, so an asset CDN permanent
redirect cannot pull an entire third-party host. Site-root Locations still
expand to host history when website mode is selected. The CLI default is
`--redirect-capture page`.

These CDX searches run on a dedicated single-thread expand executor, not on
the WARC coordinator. WARC workers keep filling from the pending queue while
an expand is in flight. Expands stay serial on the shared main Wayback client.
Every nonempty result is saved as source files. Only URL histories that were
not already selected from the primary search are allocated as new WARC batches
and pushed onto the live work queue. Search scopes are deduplicated, so cycles
terminate. Discovery stops when finished WARCs yield no unseen redirect
searches. Long redirect CDX pulls print a start line and periodic fetch
progress so they are not mistaken for a stalled WARC build.

Redirect responses are downloaded once for final WARC storage; there is no
separate discarded probe pass. Existing valid WARC responses may still be
reused during a rebuild and still contribute Location targets.

## WARC ownership

The handoff to WARC construction uses three concrete values:

```python
@dataclass(frozen=True)
class UrlHistory:
    domain: str
    urlkey: str
    warc_captures: tuple[CdxRecord, ...]
    website_files: tuple[WebsiteFile, ...]

@dataclass(frozen=True)
class WarcBatch:
    path: Path
    histories: tuple[UrlHistory, ...]

@dataclass(frozen=True)
class WebsiteBatch:
    history: UrlHistory
```

A `WarcBatch` contains everything its worker needs. The worker does not look
up URL keys in collection-wide capture dictionaries.

One worker owns one `WarcBatch` from start to finish:

1. inventory an existing WARC when present;
2. replace any leftover temporary WARC, then open one exclusive temporary on demand;
3. process the batch's URL histories sequentially;
4. preserve capture order and per-history digest/revisit state;
5. validate the completed temporary WARC;
6. atomically replace the final path once.

Different WARC batches run concurrently and may finish out of allocation
order. Redirect expansion is scheduled off the coordinator when a finished
expandable WARC (`WarcBatch.expand=True`) yields Location targets; additional
batches are appended when that expand completes, and the completion counter's
denominator grows when that happens. Same-site targets (apex and subdomains of
the seed pattern) keep ``expand=True``. Off-site targets use ``expand=False``:
their 301/308 responses are still stored in WARC, but are not expanded further
(one hop beyond the seed site).
A URL history never has two WARC owners. Histories selected for both WARC and
loose-file output stay attached to the WARC batch and use the same downloaded
body. Histories selected only for files run as `WebsiteBatch` values in a
separate phase.

The existing behavior remains authoritative for:

- response and revisit ordering;
- digest normalization and representative selection;
- full-response storage for redirects;
- playback status validation;
- existing-WARC response reuse;
- loose-file reuse and MIME path validation;
- partial failures;
- atomic WARC replacement.

## Replay index

After every WARC is validated and published, `build_replay_index()` runs once
as a serial final stage. It indexes response and revisit records from every
available WARC and atomically replaces:

```text
replay/index.cdxj
```

Each CDXJ `filename` is collection-relative and includes the domain folder,
for example:

```json
{"filename":"archive/example.com/index.warc.gz"}
{"filename":"archive/target.org/index.warc.gz"}
```

There are no per-WARC CDX shards. Navigator resolves WARC files solely through
the domain-folder path in the CDXJ `filename` field.

## Console output

The console reports phases and completed files, not successful captures:

```text
Fetch example.com/* (1995-20260803): WARC all, files none, redirects page, 8 workers
Search: 120 captures in 18 URL histories
WARC files: building 18 with 8 workers
[1/18] http://web.archive.org/web/*/https://example.com/
  4 responses, 3 revisits, 0 failed
Redirect: +2 histories from https://target.org/
[2/20] http://web.archive.org/web/*/https://example.com/about
  1 responses, 0 revisits, 0 failed
[3/20] http://web.archive.org/web/*/https://target.org/
  8 responses, 1 revisits, 1 failed
  https://web.archive.org/...
    truncated after 9 attempts over 12.0s (1,000/2,000 bytes)
Replay index: replay/index.cdxj from 20 WARC files
Done in 2.3 minutes: 155 selected, 120 responses, 34 revisits, 1 failed
```

Retries print immediately because a worker may wait through a long backoff.
WARC completions print a Wayback calendar URL, then an indented stats line.
Capture failures and warnings print the capture URL on one line and a single
indented detail line beneath it. File counts are appended to their owning WARC
stats line. Redirect expansion prints a short `Redirect: +N histories from <url>`
line when new WARC work is queued. File-only histories appear in a separate
`Website files` phase.

There are no successful per-capture lines, URL alignment, per-history summary
blocks, verbose mode, or second event log. The same compact output is mirrored
to the primary source log.

## Modules

| Module | Responsibility |
| --- | --- |
| `fetch.py` | Settings, phase orchestration, redirect enqueue callback |
| `search.py` | CDX search, grouping, primary selection |
| `redirects.py` | Location resolution and redirect CDX expansion helpers |
| `collection_paths.py` | Collection/domain paths and collision handling |
| `source_files.py` | Saved CDX search results and query metadata |
| `downloads.py` | Exact playback, retries, thread-private clients |
| `warc_files.py` | URL histories, WARC batches, WARC/file construction |
| `warc_records.py` | WARC serialization, validation, existing-WARC reads |
| `website_files.py` | Loose-file counts and body writes |
| `replay_index.py` | Final collection-wide CDXJ |
| `atomic_files.py` | Atomic filesystem publication |
| `local_links.py` | Optional loose-file link rewriting |
| `console.py` | Thread-safe immediate output and source-log mirroring |
| `retry.py` | Retry classification and bounded backoff |

The old module names have no compatibility shims. The CLI is the supported
public interface.

## Verification

Run Fetch and Navigator separately because both suites contain a top-level
`test_cli` module:

```bash
pytest -q archive-magic-fetch/tests
pytest -q archive-magic-navigator/tests
```

Navigator's socket integration tests require permission to bind temporary
localhost ports. Tests cover redirect status selection, recursive cycles,
bounded overlapping probes, thread-private clients, exactly-once WARC
ownership, sequential histories within a WARC, completion-order output,
domain folders, ports, IDNA, IPv6, collision handling, replay filenames, and
existing WARC reuse.
