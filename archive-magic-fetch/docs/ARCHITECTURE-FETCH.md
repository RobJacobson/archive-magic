# Archive Magic Fetch architecture

Archive Magic Fetch performs two concrete operations:

1. Search Internet Archive for each URL's captures across time.
2. Build final WARC files from those URL histories.

The implementation deliberately separates searching from writing. WARC builds
start from the primary search selection and preserve the complete validated
local baseline before adding captures currently returned by IA.

## Command line

```text
archive-magic-fetch URL_PATTERN
  [--start DATE]
  [--end DATE]
  [--build-warc true|false]
  [--files none|latest|unique|all]
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

`--build-warc` defaults to `true` and always selects the complete CDX history.
Use `false` for a loose-files-only run. There is no latest-only WARC mode.

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
├── collection.json          # merge/resume coverage envelope
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
│       ├── redirects.json
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

## Merge and resume

Repeated fetches against the same collection **merge by default**.

Before CDX search, Fetch loads prior coverage from `collection.json` when
present. The effective search window is the union of prior coverage and the
current `--start`/`--end`:

```text
min(prior.date_start, --start) … max(prior.date_end, --end)
```

That expanded window is used for the primary CDX search. The operator still
sees the request dates on the `Fetch` line; expansion is reported as:

```text
Merge: expanding search 2005-2010 using prior coverage 1995-2005 -> 1995-2010
```

After WARC construction completes, coverage is rewritten to the effective
window plus `url_pattern` and `files_mode`. WARC output is not part of coverage
identity. Older coverage schema versions are rejected rather than migrated.

Staging `1995–2000` then `2000–2005` is therefore equivalent to one `1995–2005`
search for stable IA CDX data. Extending `--end` a month later re-searches the
union window. The desired collection is the semantic union of the validated
local WARC inventory and current IA CDX rows. IA removals therefore never
delete local captures. Unchanged WARCs are retained byte-for-byte; affected
WARCs are rebuilt through a temporary and atomically replaced.

## Search and selection

`search_captures()` materializes a complete CDX result within the effective
(merged) date bounds. Every nonempty search is saved under `sources/` before
it is used downstream.

`group_by_url()` groups records by:

```text
(normalized domain folder, CDX urlkey)
```

and orders each history by capture time. WARC output retains every logical
capture. `select_captures()` applies only the loose-file modes:

- `none`: select nothing;
- `all`: retain every capture;
- `unique` for files: retain the full history so digest reuse can choose
  representative bodies;
- `latest`: prefer the newest 200, then the newest known non-redirect,
  then the newest known redirect.

WARC and loose-file selection happens before construction. Redirect targets
never introduce CDX searches or additional WARC work.

## Redirect reporting

Every selected historical 3xx response is stored with its actual status and
`Location` header. Fetch does not follow that Location or broaden capture
scope. After final WARC and replay-index construction it scans the complete
collection and writes a versioned `redirects.json` beside the run's source
query and log.

The report resolves relative HTTP(S) Locations, removes fragments, aggregates
occurrences by normalized target URL, and classifies the exact target as
already covered or skipped. It covers 301, 302, 303, 307, 308, and any other
3xx with a usable Location; 304 is excluded. Missing or invalid Locations are
listed separately. The operator can use skipped targets to choose a subsequent
explicit Fetch query.

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

1. validate and inventory every existing collection WARC;
2. return an unchanged WARC directly when it already covers the selection;
3. otherwise copy its response/revisit baseline to an exclusive temporary;
4. process new logical captures sequentially with normalized cache lookup;
5. validate the temporary and assert every prior identity remains present;
6. atomically replace the final path once.

Different WARC batches run concurrently and may finish out of allocation
order.
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
as a serial final stage. It indexes response and revisit records from **every**
final `*.warc.gz` under `archive/` (not only WARCs rewritten in this run) and
atomically replaces:

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
the domain-folder path in the CDXJ `filename` field. Indexing the full tree
keeps URLs that were only built in earlier stages in the replay index.

## Console output

The console reports phases and completed files, not successful captures:

```text
Fetch example.com/* (1995-20260803): build WARC true, files none, 8 workers
Search: 120 captures in 18 URL histories
WARC files: building 18 with 8 workers
[1/18] http://web.archive.org/web/*/https://example.com/
  4 responses, 3 revisits, 0 failed
[2/18] http://web.archive.org/web/*/https://example.com/about
  1 responses, 0 revisits, 0 failed
[3/18] http://web.archive.org/web/*/https://example.com/contact
  8 responses, 1 revisits, 1 failed
  https://web.archive.org/...
    truncated after 9 attempts over 12.0s (1,000/2,000 bytes)
Replay index: replay/index.cdxj from 18 WARC files
Redirects: 2 targets skipped, 1 already captured, 1 unresolved; sources/.../redirects.json
Done in 2.3 minutes: 155 selected, 120 responses, 34 revisits, 1 failed
```

Retries print immediately because a worker may wait through a long backoff.
WARC completions print a Wayback calendar URL, then an indented stats line.
Capture failures and warnings print the capture URL on one line and a single
indented detail line beneath it. File counts are appended to their owning WARC
stats line. The final redirect summary points to the durable report. File-only
histories appear in a separate `Website files` phase.

There are no successful per-capture lines, URL alignment, per-history summary
blocks, verbose mode, or second event log. The same compact output is mirrored
to the primary source log.

## Modules

| Module | Responsibility |
| --- | --- |
| `fetch.py` | Settings and phase orchestration |
| `search.py` | CDX search, grouping, primary selection |
| `redirects.py` | Final-collection redirect resolution and reporting |
| `collection_paths.py` | Collection/domain paths and collision handling |
| `collection_coverage.py` | Merge/resume coverage envelope and date window union |
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
localhost ports. Tests cover redirect reporting, append-only WARC unions,
normalized cache identity, thread-private clients, exactly-once WARC
ownership, sequential histories within a WARC, completion-order output,
domain folders, ports, IDNA, IPv6, collision handling, replay filenames, and
existing WARC reuse.
