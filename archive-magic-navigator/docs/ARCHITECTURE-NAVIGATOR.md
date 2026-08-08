# Archive Magic Navigator Architecture

**Status:** Implemented

**Scope:** `archive-magic-navigator` only

**Updated:** August 2, 2026

**Compatibility spike:** [PYWB-SPIKE.md](PYWB-SPIKE.md)

## 1. Decision

`archive-magic-navigator` is a local Python CLI that presents the WARC and CDXJ
collections produced by `archive-magic-fetch` through pywb's browser-based
archive viewer.

The public name is **Archive Magic Navigator**. "Navigator"
suggests moving through both a website and its history, and it gives the
application a distinctive identity while remaining generic enough not to imply
an association with another archive product. "Replay" remains appropriate as
an internal and ecosystem-facing technical term, but it is not the product
name.

Navigator's current scope is deliberately small:

- serve existing Archive Magic collections from the local filesystem;
- use the Internet Archive's Memento endpoint as the default fallback for
  resources missing from a local collection;
- support one selected collection or all immediate collections, failing startup
  if any selected collection is invalid;
- use pywb's framed replay, timeline, calendar, URL search, and rewriting;
- provide a minimal branded home/collection experience as an example of pywb
  customization;
- bind only to the local machine by default;
- perform no recording, crawling, indexing, or repair;
- keep the collection tree read-only; and
- run the pinned pywb release as a separate process behind an Archive Magic
  CLI boundary.

The two applications remain independently runnable:

```text
archive-magic-fetch https://example.com/*
archive-magic-navigator example.com
archive-magic-navigator --all
```

They do not import each other's Python packages or invoke each other's CLIs.
Their integration boundary is the documented collection layout.

### 1.1 Current decisions

| Area | Decision |
| --- | --- |
| Product name | Archive Magic Navigator |
| Repository/distribution/CLI | `archive-magic-navigator` |
| Import package | `archive_magic_navigator` |
| Runtime language | Ordinary CPython 3.12 |
| Python range | Navigator `>=3.12,<3.13` |
| Replay engine | Stable `pywb==2.9.1` |
| Pywb integration | Separate child process through supported CLI/config |
| Preferred pywb executable | `wayback` from Navigator's Python environment |
| Navigator license | MIT, subject to final packaging review |
| Data source | Local WARC/CDXJ first; Internet Archive Memento fallback by default |
| Collection ownership | Fetch owns; Navigator reads |
| Collection writes | Forbidden |
| Live-web fallback | Forbidden |
| Wayback fallback | Enabled by default; `--wayback-fallback off` disables it |
| Fallback persistence/cache | None |
| Recording/crawling | Forbidden |
| Auto-indexing | Forbidden |
| Default bind | `127.0.0.1` |
| Default port | `8080` |
| Initial UI | Pywb built-in UI with light example branding |
| Initial landing behavior | Root landing page, not a forced first URL |
| Browser opening | `--open` is opt-in |
| Concurrent Fetch writes | Unsupported |
| S3/R2 | Future work only |
| React frontend | Future work only |

## 2. Goals and principles

### 2.1 Goals

Navigator:

1. replays HTML and supporting archived resources from local WARC records,
   falling back to the Wayback Machine when a resource is missing locally;
2. exposes multiple captures of a URL through pywb's timeline/calendar UI;
3. navigates between older and newer captures supported by the replay index;
4. shows a minimal landing page instead of immediately opening one archived
   URL;
5. exposes a pywb-backed multi-collection picker when serving all collections;
6. makes safe, quiescent replacements at an existing WARC or CDXJ path visible
   without maintaining a private authoritative copy;
7. leaves every file under the selected `archives/` tree unchanged; and
8. fails clearly when the input collection is missing, malformed, unsafe, or
   incompatible.

### 2.2 Principles

The implementation follows these principles:

- **Read-only consumer:** Fetch owns archive publication. Navigator reads it.
- **Data contract, not code coupling:** Collection paths and CDXJ fields are
  the interface between applications.
- **Local-first archive replay:** A valid local capture is authoritative. A
  missing resource may be loaded from the Internet Archive, but never from the
  current live web.
- **Pywb as a product dependency:** Navigator configures and launches pywb
  rather than reimplementing its replay engine.
- **Public terminology may differ from implementation terminology:** The UI
  may say "snapshot", "version", "browse", and "history" while code and pywb
  documentation use "capture", "memento", and "replay".
- **Local safety first:** The development server binds to loopback and is not
  presented as a production internet deployment.
- **No hidden repair:** Invalid input produces a diagnostic. Navigator never
  rewrites an index or WARC behind the user's back.

## 3. Application boundaries

The applications have separate ownership:

| Area | Owner | Navigator behavior |
| --- | --- | --- |
| `sources/` | Fetch | Ignore |
| `archive/**/*.warc.gz` | Fetch | Read by indexed byte range |
| `indexes/index.cdxj` | Fetch | Query as the authoritative replay index |
| `website/` | Fetch | Ignore |
| Navigator runtime config | Navigator | Generate outside the collection tree |
| Navigator templates/static assets | Navigator | Ship inside the Navigator package |
| Browser preferences/history | Future Navigator | Store outside the collection tree |

Fetch is responsible for:

- capture discovery and selection;
- durable reporting of covered and skipped historical redirect targets;
- network retrieval and retry;
- WARC record construction and validation;
- response/revisit identity;
- compressed member boundaries;
- deterministic collection-relative WARC filenames;
- sorted CDXJ generation from the final WARC bytes; and
- publication of the WARC/CDXJ data.

Navigator is responsible for:

- collection selection and read-only validation;
- translating the Archive Magic layout into pywb configuration;
- configuring the optional Internet Archive Memento fallback;
- pywb process lifecycle and local server options;
- replay-oriented console errors;
- minimal UI branding; and
- browser-facing routes.

Fetch preserves redirect responses without broadening capture scope. Its
`redirects.json` report lets an operator select target sites for later Fetch
runs. A locally captured target is authoritative and prevents remote lookup;
fallback covers targets and assets that are absent from the collection.
Navigator does not depend on how a local record was selected.

Navigator does not depend on `archive-magic-fetch`. A user may copy a valid
Archive Magic collection to a machine that has only Navigator installed and serve
it there.

Navigator must not:

- import `archive_magic_fetch` or invoke its CLI;
- create, regenerate, sort, merge, convert, or repair WARC or CDXJ;
- run `wb-manager`;
- move or copy the collection into a pywb-managed layout;
- write metadata, indexes, templates, static files, logs, or caches beneath
  `archives/`;
- enable pywb recording, live, proxy, auto-fetch, or auto-indexing modes;
- treat `sources/` or `website/` as replay inputs;
- infer capture timestamps or target URLs from WARC filenames; or
- add a shared Fetch/Navigator Python library.

The repository root remains a uv workspace and development convenience. It
does not turn Fetch and Navigator into one application.

## 4. Collection data contract

Navigator consumes the Fetch collection layout:

```text
archives/
└── example.com/
    ├── sources/
    ├── archive/
    │   └── 2004/
    │       ├── example.com-2004-001.warc.gz
    │       └── example.com-2004-002.warc.gz
    └── indexes/
        ├── years/
        │   └── 2004.cdxj
        └── index.cdxj
```

A locally replayable collection has, at minimum:

```text
<collection-root>/indexes/index.cdxj
<collection-root>/<each CDXJ filename>
```

Each non-empty CDXJ line has three whitespace-separated fields:

```text
<SURT-url-key> <14-digit-timestamp> <JSON-object>
```

Fetch's CDXJ entries use collection-relative filenames:

```json
{
  "filename": "archive/example.com/posts/index.warc.gz",
  "offset": "9673",
  "length": "9362"
}
```

| Field | Meaning | Validation |
| --- | --- | --- |
| `filename` | Collection-relative WARC path | Normalized relative POSIX path beginning `archive/<domain-folder>/` |
| `offset` | Compressed-member byte offset | Integer string or integer, value `>= 0` |
| `length` | Compressed-member byte length | Integer string or integer, value `> 0` |

Consequently, pywb's resource prefix must be the collection root, not the
`archive/` directory:

```text
resource prefix + filename
= /absolute/path/archives/example.com/
  + archive/example.com/posts/index.warc.gz
```

The CDXJ, not the readable WARC filename, defines capture identity and the
compressed byte range to load. Navigator does not infer a target URL or timestamp
from a WARC path.

Fetch guarantees that:

- the index is sorted by URL key and timestamp;
- `filename` is relative to the collection root;
- offsets and lengths address independently compressed WARC members;
- response and revisit records are indexed;
- revisits may reference full responses in the same year or an earlier year
  under other `archive/YYYY/` paths; and
- completed WARC files and the CDXJ file are each published with atomic
  replacement.

Playback therefore requires the full collection chain, not a single annual
slice of WARCs. Annual `indexes/years/YYYY.cdxj` files remain helpful for
recovery but are not independently portable when revisits cross year
boundaries.

### 4.1 Required preflight

Before starting pywb, Navigator validates the selected input without changing it:

1. The archives root and selected collection are directories.
2. The collection resolves beneath the configured archives root.
3. `indexes/index.cdxj` resolves beneath the collection root and is a regular,
   readable UTF-8 file.
4. Each non-empty CDXJ line splits into a URL key, 14-digit timestamp, and JSON
   object.
5. CDXJ keys and timestamps are nondecreasing.
6. Each replay record has a relative `filename` and nonnegative integer
   `offset` and positive integer `length` (numeric strings or JSON integers;
   not booleans, floats, or signed forms).
7. A filename is a normalized relative POSIX path, begins with
   `archive/<domain-folder>/`, contains no `.` or `..` traversal, and resolves
   beneath the collection root.
8. Each distinct referenced WARC is a readable regular file, and
   `offset + length` does not exceed the current file size.
9. Symlinks or resolved targets that escape the collection are rejected.
10. An entirely empty index is rejected.

Validation streams the index line by line. Diagnostics include the collection
ID, index path, and one-based line number without dumping the full JSON
record. Distinct WARC filenames are validated once and cached for the rest of
the index.

Navigator does not fully parse every WARC at startup. Fetch already performs WARC
validation, and eagerly decompressing all records would make server startup
unnecessarily expensive. Integration tests verify that pywb can load the
indexed byte ranges.

### 4.2 Future collection manifest

Navigator does not require a new manifest. Folder names and the existing CDXJ
are sufficient for the local filesystem mode.

A future storage/concurrency revision should add a small versioned manifest
that identifies:

- the Archive Magic collection-format version;
- a stable collection ID and optional display title;
- the current immutable generation;
- the replay index object/path for that generation;
- the WARC resource prefix;
- an optional preferred starting URL; and
- integrity metadata such as sizes, ETags, or hashes.

Navigator must retain a manifest-less local mode for existing Fetch
collections.

## 5. CLI contract

The commands are:

```text
archive-magic-navigator COLLECTION
  [--archives PATH]
  [--bind ADDRESS]
  [--port PORT]
  [--wayback-fallback {on,off}]
  [--open]
  [--debug]

archive-magic-navigator --all
  [--archives PATH]
  [--bind ADDRESS]
  [--port PORT]
  [--wayback-fallback {on,off}]
  [--open]
  [--debug]
```

The executable and distribution are both named `archive-magic-navigator`; the
import package uses Python's underscore form, `archive_magic_navigator`.

| Option | Default | Meaning |
| --- | --- | --- |
| `COLLECTION` | none | One collection directory name beneath `--archives` |
| `--all` | off | Validate and serve every immediate collection beneath `--archives`; fail if any is invalid |
| `--archives` | `./archives` | Shared Archive Magic collection root |
| `--bind` | `127.0.0.1` | Local address on which pywb listens |
| `--port` | `8080` | Local TCP port |
| `--wayback-fallback` | `on` | Load resources missing locally from the Internet Archive; `off` provides strictly local replay |
| `--open` | off | Open the landing page in the default browser |
| `--debug` | off | Send pywb diagnostic output directly to the console |

Exactly one of `COLLECTION` and `--all` is required. A collection positional
argument is an ID, not an arbitrary path. This keeps route names and filesystem
resolution separate. Users with a nonstandard location pass its parent with
`--archives`.

Collection IDs use the deliberately narrow route-safe grammar
`[A-Za-z0-9][A-Za-z0-9._-]*`. Navigator rejects pywb-reserved route names such
as `static`. This prevents filesystem names containing URL delimiters or
control characters from producing broken links and prevents collection routes
from colliding with pywb's own endpoints.

The command prints the landing-page URL after startup:

```text
Archive Magic Navigator
Serving 1 collection from /path/to/archives
Wayback fallback: on
Open http://127.0.0.1:8080/
Press Ctrl-C to stop.
```

| Condition | Exit |
| --- | --- |
| Success / clean Ctrl-C | `0` |
| Argument parser error | `2` |
| Validation or startup failure | `1` |
| Pywb exits nonzero after launch | Preserve a positive pywb exit code where practical; otherwise `1` |

`--bind 0.0.0.0` is permitted as an explicit development choice but prints a
warning that authentication, TLS, hardened deployment, and hostile archive
review are outside Navigator's current scope.

## 6. Startup and shutdown flow

The successful process flow is:

```text
parse and validate CLI arguments
    -> resolve the archives root
    -> select one collection or discover all candidates
    -> perform read-only collection/CDXJ preflight
    -> create an ephemeral runtime directory outside archives/
    -> render local-first pywb config.yaml with optional Wayback fallback
    -> stage packaged branding templates/static assets into the runtime
    -> locate the pinned wayback executable beside Navigator's Python interpreter
    -> create a random ephemeral readiness resource
    -> start the pinned wayback executable as a child process
    -> poll that resource without HTTP proxies or detect an early child failure
    -> print the landing-page URL
    -> optionally open the browser
    -> forward interrupt/termination to pywb
    -> wait for clean shutdown
    -> remove the ephemeral runtime directory
```

Navigator owns the child process. It preserves pywb's nonzero exit status,
reports a port conflict clearly, and avoids leaving an orphan server after
Ctrl-C or ordinary termination.

The readiness resource contains a per-run random token and lives beneath the
ephemeral static directory. Navigator requires an exact response before
declaring the child ready and rechecks that the child is still running after
the response. An unrelated service already occupying the requested port
therefore cannot satisfy readiness.

The wrapper uses pywb's supported executable/configuration boundary instead
of importing pywb implementation classes. This reduces coupling to internal
Python APIs, isolates pywb's gevent monkey-patching, and provides the cleanest
license and process boundary. Executable discovery is scoped to the current
Python environment rather than global `PATH`, and Navigator verifies the exact
pinned pywb distribution version before launch.

Pywb 2.9.1 installs equivalent `wayback` and `pywb` console commands. Navigator
prefers `wayback` because the pinned source and documentation call it the main
application. The public command remains `archive-magic-navigator`.

## 7. Pywb configuration

Navigator always enables:

- framed replay;
- pywb's default replay rewriting for the pinned version;
- Memento/timeline behavior needed by the built-in viewer; and
- packaged Archive Magic templates.

`client_side_replay` remains off. There is no public "extra pywb YAML" escape
hatch; that would undermine the safety guarantees by allowing recording or live
fallback.

Navigator never enables:

- `--record` or a `recorder` block;
- `--live` or a live-web collection;
- `--autoindex` or an `autoindex` block;
- proxy mode; or
- `wb-manager`.

### 7.1 Explicit collections

Navigator generates an explicit route for every collection that passed
preflight and disables pywb's automatic collection routes. With the default
Wayback fallback, a single collection looks like:

```yaml
enable_auto_colls: false
framed_replay: true
client_side_replay: false

collections:
  example.com:
    sequence:
      - name: local
        index: /absolute/path/archives/example.com/indexes/index.cdxj
        archive_paths:
          - /absolute/path/archives/example.com/
      - name: wayback
        index_group:
          ia: memento+https://web.archive.org/web/
        timeout: 10
```

Pywb tries the sequence in order, so any valid local response—including a
redirect or archived error response—is authoritative. A missing index or
unloadable local resource falls through to the Internet Archive. Wrapping the
Memento source in an index group applies the ten-second timeout to remote
lookup and loading. `--wayback-fallback off` emits the original local-only
`index` and `archive_paths` shape.

The compatibility spike documented in [PYWB-SPIKE.md](PYWB-SPIKE.md) confirmed
the local handler against Fetch's collection-root `archive/...` filenames. A
real-pywb integration test covers the sequential fallback with a deterministic
local Memento service.

### 7.2 All collections

`--all` uses the same local-first sequence with one entry per validated
collection. Each route has its own local handler followed by the shared
Wayback Memento source:

```yaml
enable_auto_colls: false
framed_replay: true
client_side_replay: false
collections:
  example.com:
    sequence:
      - name: local
        index: /absolute/path/archives/example.com/indexes/index.cdxj
        archive_paths:
          - /absolute/path/archives/example.com/
      - name: wayback
        index_group:
          ia: memento+https://web.archive.org/web/
        timeout: 10
  example.net:
    sequence:
      - name: local
        index: /absolute/path/archives/example.net/indexes/index.cdxj
        archive_paths:
          - /absolute/path/archives/example.net/
      - name: wayback
        index_group:
          ia: memento+https://web.archive.org/web/
        timeout: 10
```

This keeps configuration generation identical in single and all-collection
modes and prevents pywb from exposing a directory that Navigator did not
validate. Replacements at configured CDXJ/WARC paths remain visible to later
pywb reads. Adding or removing an entire collection requires restarting
Navigator, which is acceptable while concurrent publication is unsupported.

Every immediate child directory under the archives root is treated as an
intended collection. If any such directory is invalid, startup fails with
diagnostics instead of allowing pywb's homepage to list a broken collection.
Non-directory entries are ignored. A later filtered dynamic-collection view
would be a separate design if mixed collection and non-collection directories
become common.

## 8. Browser UI and replay behavior

Pywb's default root homepage lists available collection routes. Its home-page
template receives the route list and collection metadata, so Navigator does not
need a separate collection-picker application.

The browser flow is:

```text
Archive Magic landing page
    -> select a collection
    -> collection URL-search page
    -> calendar/list of available captures for a URL
    -> framed replay with capture timeline
    -> older/newer captures of the current URL
```

Navigator branding demonstrates supported customization without replacing
pywb:

- an Archive Magic title and short explanation on the root home page;
- a styled collection list;
- a lightly branded collection search page;
- terminology such as "archived version" or "snapshot" where that is clearer
  to non-archivists.

Only `index.html` and `search.html` are overridden. The templates are original
minimal Jinja using documented pywb variables; they are not wholesale copies of
pywb's default GPLv3 templates. The replay banner, timeline, calendar, URL
rewriting, Wombat behavior, and capture selection remain pywb-owned.

Pywb 2.9.1's root-page Jinja loader searches `templates/` beneath the server
working directory before its packaged defaults. Navigator therefore copies its
packaged overrides and stylesheet into the ephemeral runtime directory before
startup. No template or static file is written beneath a collection.

The initial UI is functional rather than a final design. A polished homepage
and a React client are future possibilities.

## 9. Read-only and idempotent behavior

For identical arguments and identical collection files, Navigator produces the
same routes and pywb configuration.

It makes no persistent writes under:

```text
archives/
```

Ephemeral config and process files live in a temporary runtime directory.
Future user preferences, recent pages, or browser history must live in an
application-state directory outside the collection tree.

Navigator does not:

- move or copy WARC files into a pywb collection;
- convert or regenerate CDXJ;
- create pywb `indexes/`, `archive/`, `acl/`, `metadata.yaml`, or
  collection-local template directories;
- update Fetch provenance;
- append access data to WARCs; or
- use collection files as a cache directory.

Wayback responses are streamed through pywb and are neither persisted nor held
in an application-owned response cache. Backend caching is a possible future
enhancement, not part of the initial fallback.

Pywb's file index source opens the CDXJ for a query, and WARC resources are
opened when a record is requested. By pointing pywb directly at the shared
paths, Navigator avoids a stale private data copy. Atomic replacement of a file at
the same path can therefore be visible to later requests.

This observation is not a concurrency guarantee. See the next section.

## 10. Consistency and safe concurrency

### 10.1 Current rule

Early development assumes there is no writer modifying a served collection.
Fetch and Navigator may be installed and run independently, but a Fetch
publication must not overlap replay requests for that collection.

Navigator does not enforce this assumption with a long-lived lock. The
limitation is documented in the README; there is no runtime lock or
concurrency warning.

### 10.2 Why current atomic files are insufficient

Fetch atomically replaces each completed WARC and later atomically replaces
`indexes/index.cdxj`. Those individual publications are safe, but the collection
is not one transaction.

During a concurrent update, these unsafe combinations are possible:

```text
old CDXJ -> new WARC at the same filename
new CDXJ -> an incompletely published set of WARC filenames
```

The first case is especially important because an old offset/length may address
unrelated compressed bytes in the replacement WARC.

A shared/exclusive process lock would prevent cooperative local writers, but it
does not solve remote object storage, crashed readers, multiple hosts, or
noncooperating tools. It is useful for diagnostics, not the long-term
publication model.

### 10.3 Future publication model

Safe production concurrency should use immutable data plus an atomic pointer:

1. Fetch writes new WARC objects under immutable generation-qualified keys.
2. Fetch creates a CDXJ whose filenames identify only objects in that
   generation.
3. Fetch validates every object and the complete index.
4. Fetch publishes a small current-generation manifest with one atomic local
   replace or conditional object-store write.
5. Navigator resolves and pins one generation for a request or worker view.
6. Old generations remain available until no reader can refer to them.
7. Garbage collection is a separate, explicit retention operation.

Conceptually:

```text
example.com/
├── generations/
│   ├── 01...A/
│   │   ├── archive/...
│   │   └── indexes/index.cdxj
│   └── 01...B/
│       ├── archive/...
│       └── indexes/index.cdxj
└── current.json
```

The exact local and object-store key layout remains a future design decision.
It must preserve range-addressable gzip members and must not require mutating a
published WARC.

## 11. Future bucket and remote-resource support

Remote storage is out of Navigator's current scope, but the local architecture
must not obstruct it.

Pywb loads a WARC record using the CDXJ `filename`, `offset`, and `length`:

- local files are opened, seeked to `offset`, and limited to `length`;
- HTTP/HTTPS resources are requested with
  `Range: bytes=<offset>-<offset+length-1>`; and
- `s3://` resources use S3 `GetObject` with the equivalent `Range`.

This is the desired behavior for S3 and Cloudflare R2 because a replay request
need not transfer the complete WARC object.

The likely first remote design is:

```text
local/cacheable CDXJ index
    -> remote immutable WARC objects
    -> HTTP or S3-compatible byte-range reads
```

Pywb 2.9.1's plain file index source expects a local CDX/CDXJ file. A raw CDXJ
object URL is not a remote index API. Options for a later revision include:

1. download and atomically cache the comparatively small CDXJ locally;
2. serve the index through a pywb-compatible CDX API;
3. adopt a ZipNum/sharded index for very large collections; or
4. add a narrowly scoped index adapter.

For WARC objects:

- public or signed HTTPS object URLs can use pywb's HTTP range loader;
- AWS S3 can use pywb's S3 loader and standard credential discovery;
- R2 may use its S3-compatible endpoint or HTTPS URLs, subject to a tested
  endpoint/credential configuration; and
- credentials belong in environment/provider configuration, never in the
  collection CDXJ or committed config.

Bucket acceptance tests must verify:

- the server returns `206 Partial Content`;
- `Content-Range` matches the requested offset and length;
- only the indexed compressed member is transferred;
- missing/changed objects never fall back to the live web; Internet Archive
  fallback follows the explicit Navigator setting;
- ETag/version changes cannot mix generations; and
- retries do not silently download a full WARC when a provider ignores
  `Range`.

Pywb's HTTP loader sends a `Range` request but does not by itself prove that an
origin honored it efficiently. Provider-level tests and metrics are therefore
required before claiming bandwidth-safe bucket support.

Fetch's domain-folder WARC writer preserves these relative filename semantics.
Navigator never derives a WARC location from the captured URL.

## 12. Python and dependency policy

The runtime pins:

```text
Python >=3.12,<3.13
pywb ==2.9.1
setuptools >=68,<81
```

Pywb 2.9.1 is the current stable release and declares Python
`>=3.7,<3.13`. Pywb 2.10.0b1 is a prerelease, and the development branch now
declares `>=3.9,<3.15`, but Navigator does not depend on a prerelease or an
unreleased commit.

The published pywb 2.9.1 console entry point imports `pkg_resources`. A fresh
uv environment does not install setuptools by default, so Navigator declares
`setuptools>=68,<81` as a direct runtime dependency. PyYAML is also declared
directly because Navigator serializes configuration with `yaml.safe_dump()`.

The delay in newer-Python support was caused by ordinary compatibility work,
not a stated architectural problem with newer Python:

- Python 3.13 removed the deprecated standard-library `cgi` module, requiring
  pywb to adopt `legacy-cgi`;
- setuptools is no longer present by default in newer environments and had to
  become explicit;
- Python 3.13/3.14 required compatible gevent and greenlet versions;
- Python 3.14 required a newer Werkzeug; and
- Flask API changes required pywb JSON-handling updates.

Pywb merged tested Python 3.13 and 3.14 support in April 2026. That support has
not yet appeared in a stable release.

Python 3.13 and 3.14 add useful incremental language/runtime work, including
optional free-threaded builds and experimental/runtime performance features.
None is important to this wrapper or to normal pywb replay. Navigator uses the
ordinary GIL-enabled CPython build.

Python 3.12 remains under upstream security support through October 2028. Its
age is not a material reliability problem for a local development server. The
more important reliability choice is using a released pywb/dependency set that
supports the selected interpreter.

The uv workspace uses one shared lock and the intersection of member Python
ranges. Navigator therefore makes Python 3.12 the workspace development runtime
while pywb 2.9.1 is pinned. This does not require Fetch to reduce its
standalone compatibility:

```text
workspace/root:       >=3.12,<3.13
archive-magic-navigator: >=3.12,<3.13
archive-magic-fetch:  >=3.12
```

Fetch already targets Python 3.12 or newer. Running its tests and CLI on 3.12
does not change behavior. Fetch may retain separate CI coverage on a newer
Python outside the shared workspace environment if desired.

When a stable pywb release supports Python 3.13/3.14, Navigator should test that
release and lift the temporary upper bound.

### 12.1 Pywb maintenance assessment

Pywb appears active but deliberately low-velocity, which is typical for mature,
niche archival infrastructure. Evidence as of July 2026 includes:

- stable 2.9.1 released in October 2025 and beta 2.10.0b1 in November 2025;
- continuing replay-client dependency updates in April 2026;
- merged Python 3.13/3.14 compatibility in April 2026;
- recent work replacing removed packaging APIs and improving test reliability;
  and
- ongoing CI and dependency maintenance.

This is enough activity for Navigator's current wrapper, but not enough to
treat pywb as a fast-moving or low-risk dependency. The stable release cadence
lags the main branch, and the maintainer pool appears small. Navigator therefore
pins a stable version, tests real replay behavior rather than only startup, and
keeps pywb behind a replaceable executable/configuration boundary. Before a
production release, reevaluate release cadence, unresolved security issues,
browser-rewriting compatibility, and whether 2.10 has reached stable.

## 13. License and process boundary

Archive Magic intends to license its new Navigator wrapper code under MIT if
that is legally supportable. Pywb 2.9.1 is GPLv3.

Python packaging does not have a materially different license model from
JavaScript packaging. npm, pip, and uv are delivery mechanisms; none makes a
copyleft dependency permissive. JavaScript packages commonly install readable
source, and pure-Python wheels commonly install `.py` source. In either
ecosystem, the dependency license and the way the programs are combined and
distributed control the obligations.

A Navigator wheel may declare pywb as a dependency without copying pywb into
the Navigator wheel. The installer then resolves pywb as a separate
distribution. Merely installing and privately running the packages does not
require Navigator's source to be published. Pywb uses GPLv3, not AGPL, so
letting users access a privately run pywb server over a network does not by
itself trigger an AGPL-style source-offer requirement.

The harder question is whether Navigator and pywb form one combined program.
Importing and extending pywb internals inside the same Python process creates a
stronger combined-work argument. Invoking the unmodified `pywb`/`wayback`
executable with a generated YAML file and ordinary command-line/process control
creates a clearer separation:

```text
MIT Archive Magic CLI
    -> config.yaml + command-line arguments
    -> separate GPLv3 pywb executable
```

This architecture is also technically preferable because it avoids dependence
on pywb's internal Python APIs.

For an ordinary source or wheel release, Navigator should:

- identify pywb as a separate GPLv3 runtime dependency;
- avoid vendoring or copying pywb into the Navigator distribution;
- retain attribution and a link to pywb's license; and
- avoid implying that pywb itself is MIT licensed.

If Archive Magic publishes a Docker image, standalone executable, installer,
or other bundle containing pywb, it is distributing pywb and must preserve the
applicable notices and satisfy GPLv3 source/licensing obligations for that
component. MIT is GPL-compatible, but that does not permit describing a
combined GPL-covered distribution as wholly MIT.

This document is an engineering boundary, not legal advice. The final
packaging should receive a license review. If the implementation eventually
imports pywb internals or ships a tightly integrated combined executable,
licensing the combined Navigator application under GPLv3 would be the
conservative choice. There is no technical preference for GPLv3 if the
separate-process MIT design is confirmed to be compliant.

Fetch remains MIT and independent regardless of the Navigator decision.

## 14. Security posture

Archived pages can contain hostile or obsolete HTML and JavaScript. Navigator:

- binds to `127.0.0.1` by default;
- uses pywb's recommended framed replay;
- does not expose a live-web proxy or recorder;
- sends locally missing replay requests to the Internet Archive when Wayback
  fallback is enabled;
- validates CDXJ WARC paths before serving;
- rejects collection traversal and escaping symlinks;
- treats collection data as untrusted input;
- does not serve arbitrary loose files from `website/` or `sources/`; and
- does not claim internet-facing production hardening.

Framed replay reduces the ability of archived content to tamper with the
viewer banner, but it is not a complete sandbox. Public deployment requires a
separate threat model covering TLS, authentication/authorization, CSP,
reverse-proxy behavior, rate limiting, access controls, secrets, logs, and
pywb security updates.

## 15. Project structure

The implemented flat package is:

```text
archive-magic-navigator/
├── LICENSE
├── README.md
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE-NAVIGATOR.md
│   └── PYWB-SPIKE.md
├── src/
│   └── archive_magic_navigator/
│       ├── __init__.py
│       ├── cli.py
│       ├── collections.py
│       ├── config.py
│       ├── errors.py
│       ├── process.py
│       ├── validation.py
│       ├── templates/
│       │   ├── index.html
│       │   └── search.html
│       └── static/
│           └── archive-magic.css
└── tests/
    ├── fixtures/
    ├── test_cli.py
    ├── test_collections.py
    ├── test_config.py
    ├── test_process.py
    ├── test_validation.py
    └── test_pywb_integration.py
```

| File | Responsibility |
| --- | --- |
| `cli.py` | Arguments, diagnostics, exit status, high-level lifecycle |
| `collections.py` | Resolve/select/discover collection roots |
| `validation.py` | Read-only CDXJ and WARC-path preflight |
| `config.py` | Deterministic pywb YAML/runtime configuration |
| `errors.py` | Expected user-facing validation and startup failures |
| `process.py` | Child process, readiness, signals, cleanup |
| `templates/` | Minimal supported pywb UI overrides |
| `static/` | Minimal Archive Magic branding |

There is no generic storage abstraction. The local path model is kept narrow,
while configuration and validation avoid assumptions that would prevent a later
remote source implementation.

## 16. Failure policy

Before pywb starts, user-correctable input errors are concise:

```text
ERROR: collection 'example.com' cannot be resolved: .../example.com
ERROR: collection 'example.com' replay index escapes or cannot be resolved: ...
ERROR: collection 'example.com', index ..., line 42: unsafe WARC filename ...
ERROR: port 8080 is already in use on 127.0.0.1
```

After pywb starts:

- a resource missing locally is requested from the Internet Archive when
  fallback is on, and remains a pywb replay 404 if no Memento exists;
- a malformed or unloadable indexed WARC record may fall through to the
  Internet Archive, but Navigator never repairs or changes the local record;
- an unavailable remote Memento may produce pywb's archive-unavailable error;
- an unexpected child-process exit is a Navigator command failure;
- Ctrl-C requests clean child shutdown and exits without a traceback; and
- temporary runtime files are cleaned on all handled exits.

Debug mode sends pywb's stdout and stderr directly to the console. Without
`--debug`, Navigator continuously drains that output into a bounded in-memory
tail, writes at most 256 KiB to the ephemeral runtime directory, and includes
the final 20 lines in relevant startup errors.

## 17. Testing and acceptance

Unit tests cover:

- CLI exclusivity of `COLLECTION` and `--all`;
- loopback defaults and explicit non-loopback warnings;
- deterministic collection discovery and route names;
- collection containment and symlink rejection;
- CDXJ parsing, ordering, numeric ranges, and filename safety;
- config generation for one and all collections;
- default-on and explicitly disabled Wayback fallback configuration;
- no recording/live/autoindex settings;
- child startup failure, port conflict, interrupt, and exit propagation;
- child-specific, proxy-free readiness;
- interpreter-local executable and pinned-version discovery;
- bounded diagnostic output capture;
- configuration and packaged assets staged outside the archives root; and
- rejection of route-unsafe and reserved collection IDs.

Integration tests use real pywb 2.9.1 and a small real WARC/CDXJ fixture to
verify:

- the root landing page renders;
- all-collection mode lists available collections;
- a collection landing/search page renders;
- URL capture results include multiple timestamps and timestamped replay loads
  the expected bodies;
- HTML subresources are rewritten and replayed from the collection;
- response and revisit entries both replay;
- CDXJ `filename`, `offset`, and `length` select the exact compressed member;
- local captures take precedence without contacting the Memento source;
- an archived cross-host redirect and its fallback assets replay through a
  deterministic local Memento service;
- fallback-off mode leaves missing resources missing;
- live-web fallback remains absent; and
- the collection tree is byte-for-byte unchanged after the test.

A manual browser smoke test confirmed that pywb's calendar/history and framed
navigation controls render against the real local collection. Automatic
visibility of safe, quiescent replacements at existing CDXJ/WARC paths remains
an architectural requirement; it should receive a dedicated integration test
before Navigator supports concurrent publication.

A future remote fixture uses an HTTP server that records requests and rejects
full-object reads. It verifies the exact `Range` header before bucket support is
accepted.

Validation commands:

```bash
uv --directory archive-magic-navigator run pytest
uv lock --check
uv run --package archive-magic-navigator archive-magic-navigator --help
git diff --check
```

## 18. Current capabilities and future work

### Implemented

- [x] Verify pywb 2.9.1 against Fetch's WARC/CDXJ layout.
- [x] Add the Python 3.12 Navigator workspace package and CLI.
- [x] Implement single- and all-collection selection and validation.
- [x] Generate explicit configuration only for validated collections.
- [x] Supervise the pywb child through startup, interrupt, and shutdown.
- [x] Add minimal branded landing and collection-search pages.
- [x] Add deterministic response/revisit fixtures and real pywb integration
      coverage.
- [x] Prove normal replay leaves the archives tree unchanged.
- [x] Document installation, security limitations, licensing boundaries, and
      the no-concurrent-writes rule.

### Remaining before broader distribution

- [ ] Add a dedicated integration test for safe, quiescent replacement at an
      existing CDXJ/WARC path.
- [ ] Reassess the pinned pywb release and supported Python range.
- [ ] Complete legal review for any distribution model that bundles pywb.

### Future: safe concurrency

- [ ] Specify a versioned collection manifest.
- [ ] Change Fetch publication to immutable generation-qualified WARC keys.
- [ ] Publish a generation index only after all referenced WARCs exist.
- [ ] Atomically/conditionally switch the current-generation pointer.
- [ ] Pin a generation for each Navigator request/worker view.
- [ ] Define old-generation retention and garbage collection.
- [ ] Add concurrent Fetch/Navigator fault-injection tests.

### Future: S3 and R2

- [ ] Define deterministic remote object keys shared by Fetch and Navigator.
- [ ] Choose local CDXJ cache, remote CDX API, or ZipNum index strategy.
- [ ] Implement credential/provider configuration outside collection data.
- [ ] Verify S3 `GetObject Range` behavior.
- [ ] Verify R2 S3-compatible and/or HTTPS range behavior.
- [ ] Reject or loudly diagnose origins that ignore ranges.
- [ ] Add ETag/version/generation consistency checks.
- [ ] Measure transferred bytes to prevent full-WARC regressions.

### Future: product UI

- [ ] Design a polished Archive Magic homepage and collection catalog.
- [ ] Add preferences and last-page-viewed outside collection storage.
- [ ] Evaluate a separate React frontend using pywb/Warcserver APIs as the
      backend.
- [ ] Preserve a non-React built-in pywb mode for diagnostics and fallback.

### Future invariants

Although not implemented now, current design choices must not contradict these
future requirements:

- WARC objects must remain byte-range addressable by CDXJ offset and length.
- Remote WARC objects should be immutable and generation-qualified.
- A future manifest should atomically select a complete generation.
- Navigator should pin one generation for the duration of a request/worker
  view.
- Remote credentials must remain outside collection metadata.
- S3/R2 acceptance tests must prove `206 Partial Content` and transferred byte
  counts.
- Fetch and Navigator must continue to install and run independently.

## 19. Explicit non-goals

Navigator does not include:

- WARC or CDXJ creation;
- automatic indexing, reindexing, conversion, or repair;
- Internet Archive discovery or persistent download;
- live-web fallback, recording, proxy capture, or auto-fetch;
- concurrent Fetch publication;
- S3, R2, or other remote storage;
- a production public deployment;
- authentication or multi-user preferences;
- a custom React frontend;
- a final visual design;
- WACZ support;
- sharded/ZipNum indexes;
- collection deletion or garbage collection;
- changing the existing Fetch collection layout; or
- a shared Python library between Fetch and Navigator.

## 20. Remaining architecture decisions

1. Whether the final distribution model sufficiently separates the MIT wrapper
   and GPLv3 pywb process; legal review is required.
2. Whether a versioned collection manifest should arrive with safe concurrency
   or earlier as optional metadata.

## References

- [Archive Magic Fetch architecture](../../archive-magic-fetch/docs/ARCHITECTURE-FETCH.md)
- [pywb compatibility spike](PYWB-SPIKE.md)
- [pywb usage documentation](https://pywb.readthedocs.io/en/latest/manual/usage.html)
- [pywb configuration documentation](https://pywb.readthedocs.io/en/latest/manual/configuring.html)
- [pywb template guide](https://github.com/webrecorder/pywb/blob/main/docs/manual/template-guide.rst)
- [pywb indexing documentation](https://pywb.readthedocs.io/en/latest/manual/indexing.html)
- [pywb 2.9.1 block record loader](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/warcserver/resource/blockrecordloader.py)
- [pywb 2.9.1 local, HTTP range, and S3 range loaders](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/utils/loaders.py)
- [pywb 2.9.1 file index source](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/warcserver/index/indexsource.py)
- [pywb releases](https://github.com/webrecorder/pywb/releases)
- [pywb main-branch history](https://github.com/webrecorder/pywb/commits/main/)
- [pywb Python 3.13/3.14 support](https://github.com/webrecorder/pywb/pull/991)
- [pywb development-branch Python requirement](https://github.com/webrecorder/pywb/blob/main/setup.py)
- [CPython version status](https://devguide.python.org/versions/)
- [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html)
- [Python Packaging User Guide: licensing examples](https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/)
- [Python wheel specification](https://packaging.python.org/en/latest/specifications/binary-distribution-format/)
- [Python 3.13 release highlights](https://docs.python.org/3/whatsnew/3.13.html)
- [Python 3.14 release highlights](https://docs.python.org/3/whatsnew/3.14.html)
- [uv workspace documentation](https://docs.astral.sh/uv/concepts/projects/workspaces/)
