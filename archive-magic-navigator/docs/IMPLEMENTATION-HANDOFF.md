# Archive Magic Navigator Implementation Handoff

**Status:** Ready for implementation

**Audience:** Implementing agent or engineer

**Updated:** July 28, 2026

**Primary design:** [ARCHITECTURE-NAVIGATOR.md](ARCHITECTURE-NAVIGATOR.md)

## 1. Assignment

Implement the Phase 2 proof of concept described in
`ARCHITECTURE-NAVIGATOR.md`.

The deliverable is an independently runnable Python CLI named
`archive-magic-navigator`. It must serve the WARC/CDXJ collections produced by
`archive-magic-fetch` through pywb's built-in browser viewer without modifying,
copying, reindexing, or repairing the collection data.

The implementation should prove all of the following:

1. An existing Archive Magic collection can be opened in a browser.
2. Archived HTML and its archived subresources replay through pywb.
3. Multiple captures of one URL appear in pywb's calendar/timeline.
4. Older/newer capture navigation works.
5. Response and revisit WARC records both replay.
6. One selected collection and all local collections can be served.
7. Navigator leaves the shared `archives/` tree byte-for-byte unchanged.
8. Navigator and Fetch remain separate applications joined only by their
   on-disk data contract.

Start with the dependency/replay spike in section 8. Do not scaffold the full
wrapper until the spike has demonstrated that pywb 2.9.1 can consume the
existing collection directly.

## 2. Authoritative sources and precedence

Use sources in this order when instructions conflict:

1. The user's current instructions.
2. Repository `AGENTS.md` instructions.
3. `archive-magic-navigator/docs/ARCHITECTURE-NAVIGATOR.md`.
4. This implementation handoff.
5. `archive-magic-fetch/docs/ARCHITECTURE-FETCH.md` for the producer-side data
   contract.
6. Documentation and source for the pinned pywb 2.9.1 release.
7. Current pywb documentation for general concepts.

Do not silently change the Fetch data contract to accommodate pywb. If the
pinned pywb release cannot consume Fetch's valid WARC/CDXJ output, record the
smallest reproducible incompatibility and stop for a design decision.

## 3. Current repository state

The repository is a uv workspace:

```text
archive-magic/
├── pyproject.toml
├── uv.lock
├── archives/
│   └── wecanstopthehate.org/
├── archive-magic-fetch/
└── archive-magic-navigator/
    └── docs/
        ├── ARCHITECTURE-NAVIGATOR.md
        └── IMPLEMENTATION-HANDOFF.md
```

At handoff time:

- `archive-magic-fetch` is the only implemented workspace application.
- The root project declares Python `>=3.12`.
- Fetch independently declares Python `>=3.12`.
- Navigator has documentation but no package, CLI, or tests.
- The root lockfile does not yet contain pywb.
- A real local collection exists at
  `archives/wecanstopthehate.org`.
- Its replay index is
  `archives/wecanstopthehate.org/replay/index.cdxj`.

Do not clean up unrelated generated files or modify unrelated Fetch work while
implementing Navigator.

## 4. Locked decisions

These decisions have already been made. Do not reopen them during the Phase 2
implementation.

| Area | Decision |
| --- | --- |
| Product name | Archive Magic Navigator |
| Repository/distribution/CLI | `archive-magic-navigator` |
| Import package | `archive_magic_navigator` |
| Runtime language | Ordinary CPython 3.12 |
| Python range | Navigator `>=3.12,<3.13` |
| Replay engine | Stable `pywb==2.9.1` |
| Pywb integration | Separate child process through supported CLI/config |
| Navigator license | MIT, subject to final packaging review |
| Data source | Existing local WARC/CDXJ files |
| Collection ownership | Fetch owns; Navigator reads |
| Collection writes | Forbidden |
| Live-web fallback | Forbidden |
| Recording/crawling | Forbidden |
| Auto-indexing | Forbidden |
| Default bind | `127.0.0.1` |
| Default port | `8080` |
| Initial UI | Pywb built-in UI with light example branding |
| Initial landing behavior | Root landing page, not a forced first URL |
| Browser opening | `--open` is opt-in |
| Concurrent Fetch writes | Unsupported in Phase 2 |
| S3/R2 | Future work only |
| React frontend | Future work only |

The word "replay" remains correct for the pywb operation and for the existing
`replay/index.cdxj` folder. It is not the application name.

## 5. Non-negotiable application boundary

Navigator must not:

- import `archive_magic_fetch`;
- invoke the `archive-magic-fetch` CLI;
- create or regenerate a WARC;
- create, sort, merge, convert, or repair CDXJ;
- run `wb-manager`;
- move or copy the collection into a pywb-managed layout;
- write metadata, indexes, templates, static files, logs, or caches beneath
  `archives/`;
- enable pywb recording, live, proxy, auto-fetch, or auto-indexing modes;
- treat `sources/` or `website/` as replay inputs;
- infer capture timestamps or target URLs from WARC filenames; or
- add a shared Fetch/Navigator Python library.

Navigator may:

- read and validate collection paths;
- read CDXJ incrementally;
- allow pywb to seek into indexed WARC files;
- generate an ephemeral pywb `config.yaml`;
- use packaged Navigator templates/static assets;
- create temporary process state outside `archives/`;
- start and supervise the pywb executable; and
- open the browser after pywb is ready when `--open` is present.

The integration boundary is the documented collection layout and CDXJ record
schema, not Python calls between the applications.

## 6. Producer data contract

A replayable collection has this relevant shape:

```text
<archives-root>/
└── <collection-id>/
    ├── archive/
    │   └── **/*.warc.gz
    └── replay/
        └── index.cdxj
```

Each non-empty CDXJ line has three fields:

```text
<SURT-url-key> <14-digit-timestamp> <JSON-object>
```

Example:

```text
org,wecanstopthehate)/ 20080201145454 {"url":"http://www.wecanstopthehate.org/","mime":"text/html","status":"200","digest":"sha1:...","length":"9393","offset":"280","filename":"archive/index.warc.gz"}
```

The JSON fields required by Navigator are:

| Field | Meaning | Validation |
| --- | --- | --- |
| `filename` | Collection-relative WARC path | Normalized relative POSIX path beginning `archive/` |
| `offset` | Compressed-member byte offset | Integer string or integer, value `>= 0` |
| `length` | Compressed-member byte length | Integer string or integer, value `> 0` |

Fetch guarantees that:

- the index is sorted by URL key and timestamp;
- `filename` is relative to the collection root;
- offsets and lengths address independently compressed WARC members;
- response and revisit records are indexed; and
- completed WARC files and the CDXJ file are each published with atomic
  replacement.

The pywb WARC resource prefix must therefore be the collection root:

```text
/absolute/path/archives/example.com/
    + archive/posts/index.warc.gz
```

It must not be `/absolute/path/archives/example.com/archive/`, which would
incorrectly produce `archive/archive/...`.

## 7. Proposed package layout

Create:

```text
archive-magic-navigator/
├── LICENSE
├── README.md
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE-NAVIGATOR.md
│   ├── IMPLEMENTATION-HANDOFF.md
│   └── PYWB-SPIKE.md
├── src/
│   └── archive_magic_navigator/
│       ├── __init__.py
│       ├── cli.py
│       ├── collections.py
│       ├── config.py
│       ├── process.py
│       ├── validation.py
│       ├── templates/
│       │   ├── index.html
│       │   └── search.html
│       └── static/
│           └── archive-magic.css
└── tests/
    ├── fixtures/
    │   └── collection/
    ├── test_cli.py
    ├── test_collections.py
    ├── test_config.py
    ├── test_process.py
    ├── test_validation.py
    └── test_pywb_integration.py
```

Responsibilities:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument parsing, diagnostics, orchestration, exit mapping |
| `collections.py` | Archives-root resolution, collection selection/discovery |
| `validation.py` | Streaming CDXJ and contained-WARC preflight |
| `config.py` | Deterministic pywb configuration and runtime directory setup |
| `process.py` | Executable discovery, child lifecycle, readiness, signals |
| `templates/` | Original minimal Jinja templates using documented variables |
| `static/` | Small original Archive Magic stylesheet |

Keep the modules narrow. Do not add a generic storage interface, service
container, plugin layer, database, web framework, or application-state system
in Phase 2.

## 8. Milestone 0: mandatory pywb spike

### 8.1 Purpose

The spike answers the only questions that should be resolved empirically before
the wrapper is built:

1. Does pywb 2.9.1 accept Fetch's CDXJ syntax unchanged?
2. Does a collection-root `archive_paths` value resolve the existing
   `archive/...` filenames?
3. Do response and revisit records load by their indexed byte ranges?
4. Do rewritten HTML subresources resolve from the archive?
5. Does pywb's timeline/calendar expose multiple captures?
6. Does older/newer navigation select the expected capture bodies?
7. Does automatic multi-collection discovery work with
   `collections_root`, `index_paths: replay`, and `archive_paths: .`?
8. Which supported templates can be overridden without replacing pywb's replay
   controls?

### 8.2 Environment

Use an isolated Python 3.12 environment and install exactly:

```text
pywb==2.9.1
```

Record in `docs/PYWB-SPIKE.md`:

- Python version;
- pywb version;
- exact installation command;
- exact server command;
- complete YAML used;
- URLs tested;
- response/revisit examples tested;
- results for timeline and older/newer navigation;
- any pywb warnings or tracebacks; and
- whether the collection tree changed.

Do not install a pywb development commit merely to make the spike pass.

### 8.3 Pinned executable behavior

Pywb 2.9.1 installs equivalent `wayback` and `pywb` console commands. Prefer
`wayback` internally because the pinned source and documentation call it the
main application. Navigator's public command remains
`archive-magic-navigator`.

The pinned executable supports:

```text
wayback -d RUNTIME_DIRECTORY -b BIND_ADDRESS -p PORT
```

Important defaults that Navigator must override:

- pywb defaults to binding `0.0.0.0`; Navigator must pass `127.0.0.1` unless
  the user explicitly selects another address.
- pywb reads `config.yaml` from its working directory.
- pywb defaults to port `8080`.
- framed replay defaults on, but write `framed_replay: true` explicitly.

Do not pass `--live`, `--record`, `--proxy`, `--enable-auto-fetch`,
`--autoindex`, or `--all-coll`.

### 8.4 Single-collection configuration to test

Start with:

```yaml
enable_auto_colls: false
framed_replay: true
client_side_replay: false

collections:
  wecanstopthehate.org:
    index: /ABSOLUTE/REPO/archives/wecanstopthehate.org/replay/index.cdxj
    archive_paths:
      - /ABSOLUTE/REPO/archives/wecanstopthehate.org/
```

Pywb's historical documentation sometimes shows `resource` for a custom
collection. The pinned 2.9.1 `WarcServer.load_coll()` source directly reads
`index`/`index_paths` and `archive_paths`. Use the shape that passes the pinned
spike, save it in `PYWB-SPIKE.md`, and lock it with a configuration/integration
test.

Expected routes include:

```text
/
/wecanstopthehate.org/
/wecanstopthehate.org/*/http://www.wecanstopthehate.org/
/wecanstopthehate.org/<timestamp>/http://www.wecanstopthehate.org/
```

### 8.5 All-collection configuration to test

The spike should start with pywb's automatic form to verify that the Archive
Magic layout is compatible:

```yaml
collections_root: /ABSOLUTE/REPO/archives
index_paths: replay
archive_paths: .
framed_replay: true
client_side_replay: false
```

This should cause pywb to:

- discover immediate collection directories under `archives/`;
- find their CDXJ files under `<collection>/replay/`; and
- resolve indexed filenames relative to each collection root.

For the proof of concept, treat every immediate child directory under the
archives root as an intended collection. If any such directory is invalid,
fail startup with diagnostics instead of allowing pywb's homepage to list a
broken collection. Ignore non-directory entries.

If an archives root with mixed collection and non-collection directories is
needed later, design a filtered dynamic-collection view separately. Do not add
that abstraction during the spike.

The production wrapper should then render the successfully validated
collections as explicit entries using the single-collection shape above, with
`enable_auto_colls: false`. This ensures that pywb cannot expose a collection
that was added after Navigator's validation pass and avoids maintaining two
configuration-generation paths.

### 8.6 Spike pass/fail rule

The spike passes only if:

- the collection replays without WARC/CDXJ changes;
- at least two captures of one URL can be selected;
- at least one archived subresource loads;
- at least one response record and one revisit record load, if the real
  fixture contains both;
- the collection homepage and URL-search page render;
- strict replay produces a missing-resource response rather than a live fetch;
  and
- a before/after collection-tree digest or metadata inventory is unchanged.

If response replay works but revisit replay fails, determine whether the issue
is Fetch data, a pywb configuration error, or a pywb defect. Do not work around
it by rewriting the index or WARC.

## 9. Milestone 1: workspace and package scaffold

### 9.1 Root workspace

Update the root project so that:

- `archive-magic-navigator` is a workspace member;
- the shared workspace Python intersection is `>=3.12,<3.13`;
- the root development project can install both Fetch and Navigator; and
- the existing single root `uv.lock` remains authoritative.

Fetch should retain its standalone declaration of `>=3.12`; do not add a
`<3.13` upper bound to Fetch solely because Navigator needs one.

Run the complete Fetch suite after changing the workspace and lockfile.

### 9.2 Navigator package metadata

Use the same setuptools/src-layout conventions as Fetch unless a repository
standard changes before implementation.

The project metadata should include:

```text
name: archive-magic-navigator
version: 0.1.0
requires-python: >=3.12,<3.13
license: MIT
console script:
  archive-magic-navigator = archive_magic_navigator.cli:main
```

Direct runtime dependencies should include:

```text
pywb==2.9.1
PyYAML>=6,<7
```

Pywb also depends on PyYAML, but Navigator uses YAML directly and therefore
must declare it as its own direct dependency. Use `yaml.safe_dump()` or an
equivalent safe deterministic serializer. Do not hand-concatenate unescaped
filesystem paths into YAML.

Add pytest as a development dependency. Avoid adding Click, Typer, Rich,
requests, or another framework unless a demonstrated requirement justifies it;
the standard library is sufficient for this CLI and its readiness HTTP probe.

Configure setuptools package data so the installed wheel contains the
`templates/*.html` and `static/*.css` assets. Resolve installed asset locations
with `importlib.resources`; do not assume the current working directory is the
source checkout.

Add an MIT `LICENSE` for Navigator. Identify pywb as a separate GPLv3 runtime
dependency in the README and link to its license. Do not state that the entire
dependency closure is MIT.

## 10. Milestone 2: CLI contract

Implement:

```text
archive-magic-navigator COLLECTION
  [--archives PATH]
  [--bind ADDRESS]
  [--port PORT]
  [--open]
  [--debug]

archive-magic-navigator --all
  [--archives PATH]
  [--bind ADDRESS]
  [--port PORT]
  [--open]
  [--debug]
```

Defaults:

| Argument | Default |
| --- | --- |
| `--archives` | `./archives` |
| `--bind` | `127.0.0.1` |
| `--port` | `8080` |
| `--open` | false |
| `--debug` | false |

Rules:

- exactly one of positional `COLLECTION` and `--all` is required;
- `COLLECTION` is one immediate directory name, not an arbitrary path;
- reject empty IDs, `.`/`..`, separators, absolute paths, and NUL;
- resolve `--archives` to an absolute path before validation;
- reject ports outside `1..65535`;
- permit an explicit non-loopback bind but print a clear security warning;
- print the landing URL only after the pywb server is ready;
- call `webbrowser.open()` only after readiness and only for `--open`; and
- do not print a Python traceback for an ordinary user-correctable error.

Recommended exit behavior:

| Condition | Exit |
| --- | --- |
| Success / clean Ctrl-C | `0` |
| Argument parser error | `2` |
| Validation or startup failure | `1` |
| Pywb exits nonzero after launch | Preserve a positive pywb exit code where practical; otherwise `1` |

Keep messages deterministic so tests can assert them.

## 11. Milestone 2: collection discovery and validation

### 11.1 Collection resolution

For one collection:

1. Resolve the archives root.
2. Join the validated collection ID.
3. Resolve the candidate.
4. Confirm the resolved candidate remains directly beneath the archives root.
5. Confirm it is a readable directory.

For `--all`:

1. Enumerate immediate child directories only.
2. Sort by collection ID.
3. Validate every candidate.
4. Fail with aggregated, deterministic diagnostics if any candidate is
   invalid.
5. Fail if no collection is found.

Do not recursively discover collections.

### 11.2 CDXJ streaming validation

Open `replay/index.cdxj` as UTF-8 text and validate it line by line. Do not
load a potentially large index into memory.

For every non-empty line:

1. Split into URL key, timestamp, and JSON using at most two whitespace
   splits.
2. Require all three fields.
3. Require a 14-digit timestamp.
4. Decode the JSON object.
5. Require `filename`, `offset`, and `length`.
6. Accept numeric strings or JSON integers for offset/length.
7. Reject booleans, floats, signs without digits, negatives, and zero length.
8. Compare `(url_key, timestamp)` to the previous line and reject decreasing
   order.
9. Validate the WARC filename as described below.

Reject an entirely empty index.

Diagnostics must include the collection ID, index path, and one-based line
number without dumping the full JSON record.

### 11.3 WARC filename containment

Interpret CDXJ filenames as POSIX paths regardless of the host platform.

Require:

- a relative path;
- no drive or URI scheme;
- first component exactly `archive`;
- no empty, `.` or `..` components;
- no backslash path separator;
- no NUL; and
- a final resolved target beneath the selected collection root.

For each distinct filename:

1. Resolve it beneath the collection root.
2. Reject an escaping symlink at any resolved point.
3. Require a readable regular file.
4. Optionally check that `offset + length` does not exceed the current file
   size; treat a failure as invalid input.

Cache validation results per distinct WARC filename so a large CDXJ does not
repeat filesystem work.

Do not open, decompress, or parse every WARC member during startup.

### 11.4 Read-only proof

Unit and integration tests must snapshot the collection tree before and after
Navigator runs. At minimum compare:

- relative path set;
- file type;
- byte size;
- content hash for the compact fixture; and
- modification timestamp where the platform permits reliable assertions.

No temporary file may appear beneath the archives root, even during a failed
startup.

## 12. Milestone 2: deterministic pywb configuration

Create each runtime directory with `tempfile.TemporaryDirectory()` or an
equivalent scoped facility outside the archives root.

It should contain only ephemeral Navigator/pywb state, for example:

```text
<temporary-runtime>/
├── config.yaml
└── optional-process-log
```

Prefer absolute paths in generated YAML.

After branding is added, point pywb at Navigator's installed resources with
absolute `templates_dir` and `static_dir` values. Those paths must refer to
Navigator package assets or an ephemeral runtime copy, never to a directory
inside a collection.

Configuration tests must assert:

- the single-collection and all-collection structures proven by the spike;
- `framed_replay: true`;
- `client_side_replay: false` for Phase 2;
- no `recorder`;
- no `$live` collection;
- no `autoindex`;
- no proxy block;
- no `enable_auto_fetch`;
- no path beneath `archives/` used for templates, static assets, logs, or
  temporary state; and
- stable output for identical arguments.

Do not expose a public "extra pywb YAML" escape hatch in Phase 2. It would
undermine the safety guarantees by allowing recording or live fallback.

## 13. Milestone 3: pywb process lifecycle

### 13.1 Executable

Resolve the installed `wayback` executable with `shutil.which()` and fail with
an actionable message if it is absent.

Do not import:

```python
pywb.apps.cli
pywb.apps.frontendapp
```

Do not call pywb implementation classes in-process. The CLI/configuration
boundary is intentional for maintainability, gevent isolation, and licensing.

The child command should be equivalent to:

```text
wayback
  --directory <temporary-runtime>
  --bind <address>
  --port <port>
```

Use the exact options demonstrated by the pinned spike.

### 13.2 Readiness

After starting the child:

1. Poll `Popen.poll()` for early failure.
2. Attempt a bounded HTTP GET to the expected root URL.
3. Require an HTTP response from the server, not merely an open TCP port.
4. Use a short polling interval and a bounded startup deadline.
5. On timeout, terminate the child and report the last useful child log lines.

When bound to `0.0.0.0`, probe and print a usable loopback URL rather than
attempting to browse to `0.0.0.0`.

Avoid `stdout=PIPE`/`stderr=PIPE` without active readers, which can deadlock a
long-running child. A temporary log file is a simple initial solution:

- direct child output to it;
- include a concise tail on startup failure;
- stream or expose more output in `--debug`; and
- delete it with the runtime directory after shutdown.

Never print environment variables or future storage credentials.

### 13.3 Signals and shutdown

Navigator owns the child:

- Ctrl-C requests graceful child termination;
- ordinary parent termination is forwarded where the platform supports it;
- wait for a bounded graceful interval;
- kill only after the graceful deadline;
- reap the process;
- clean the runtime directory; and
- do not leave an orphan pywb server.

Use process-group/session behavior deliberately so parent and child do not both
render noisy Ctrl-C tracebacks. Cover the supported development platforms in
tests or isolate platform-specific behavior behind small functions.

### 13.4 Port conflict

Do not rely solely on a preflight socket check because it has a race. Detect a
real child startup failure and translate the common address-in-use case to:

```text
ERROR: port 8080 is already in use on 127.0.0.1
```

Retain the pywb log detail in debug output.

## 14. Milestone 3: UI and branding

The first working server should use unmodified pywb UI. Add branding only
after replay and process lifecycle tests pass.

Pywb's documented templates include:

| Template | Role |
| --- | --- |
| `index.html` | Root page and collection list; receives `routes` |
| `search.html` | Collection landing/search page; receives `coll` and metadata |
| `query.html` | URL query/calendar result page |
| `banner.html` | Replay frame/banner |
| `head.html` | Shared non-replay page head fragment |
| `frame_insert.html` | Content inserted into framed replay |

For Phase 2:

- create original, minimal Navigator templates from the documented variables;
- do not copy pywb's default GPLv3 templates wholesale into the MIT package
  without a licensing review;
- preserve pywb's query/calendar application and replay controls;
- prefer adding small CSS/header fragments to replacing complicated replay
  templates;
- override only `index.html` and `search.html` initially;
- add a replay-frame mark only if the spike identifies a documented,
  non-destructive extension point; and
- remove a branding override if it interferes with timeline, calendar,
  rewriting, or localization.

The UI should say "snapshot", "archived version", and "history" where those
terms are clearer. Internal code may continue to say "capture" and "replay".

Do not create the future React client.

## 15. Milestone 4: tests

### 15.1 Unit tests

Unit tests should cover:

- CLI mutual exclusivity and defaults;
- invalid collection IDs;
- loopback and non-loopback behavior;
- port validation;
- deterministic sorted discovery;
- missing/empty/malformed/unsorted CDXJ;
- numeric-string and integer ranges;
- rejected booleans/floats/negative or zero ranges;
- POSIX traversal, backslash, absolute, drive, URI, and escaping-symlink paths;
- missing/non-regular WARC targets;
- one filesystem validation per distinct WARC;
- deterministic single/all YAML;
- forbidden pywb modes absent from YAML and child arguments;
- executable-not-found behavior;
- early child exit;
- readiness timeout;
- port conflict;
- Ctrl-C and termination;
- positive child exit propagation;
- temporary cleanup; and
- no writes beneath the archives root.

Mock subprocess and HTTP boundaries in unit tests. Do not start pywb in every
unit test.

### 15.2 Compact integration fixture

Create a small synthetic collection containing:

- one HTML URL with at least two timestamps and visibly different bodies;
- one archived CSS or image subresource referenced by the HTML;
- one missing subresource;
- at least one full response;
- at least one valid revisit; and
- a sorted CDXJ with collection-relative `archive/...` filenames.

Prefer a committed deterministic fixture with documented provenance over
generating a subtly different WARC on every test run. If a build script is
used, retain it and assert the generated hashes.

The fixture must not depend on Internet Archive or the live network.

### 15.3 Real pywb integration tests

Start real pywb 2.9.1 on a test port and verify with HTTP requests:

1. `GET /` renders the root page and collection route.
2. `GET /<collection>/` renders the collection search page.
3. The URL query route exposes both timestamps.
4. Each explicit timestamp route returns its expected body.
5. The HTML references a rewritten replay URL for the subresource.
6. The archived subresource returns its expected bytes.
7. The revisit capture returns the correct representation.
8. The missing subresource does not contact the live web.
9. CDXJ offset/length changes select the expected compressed member.
10. The collection tree remains unchanged.

Add an all-collection test with two compact collections and assert both appear
on the root page.

Use bounded waits and guarantee child cleanup in `finally`/fixture teardown so
a failed test cannot leave port 8080 occupied.

### 15.4 Real local archive smoke test

Keep the large `wecanstopthehate.org` collection out of deterministic CI, but
document and run a manual/local smoke command when available:

```text
uv run --package archive-magic-navigator \
  archive-magic-navigator wecanstopthehate.org
```

Check at least:

- root collection page;
- `http://www.wecanstopthehate.org/`;
- capture history for that URL;
- one nested resource;
- one PDF or other non-HTML resource; and
- one revisit if identifiable.

## 16. Documentation deliverables

The implementation is incomplete without:

- `archive-magic-navigator/README.md` with installation and first-run commands;
- a clear statement that the development server is local-only by default;
- a warning for non-loopback binding;
- the Phase 2 no-concurrent-writes limitation;
- a statement that Navigator never repairs or downloads missing archive data;
- the pywb GPLv3 dependency notice and separate-process architecture;
- `docs/PYWB-SPIKE.md` with the empirical results;
- CLI help matching the implemented behavior; and
- updated architecture text if the spike proves a documented assumption wrong.

Do not document S3/R2, safe concurrent writes, public deployment, or React as
implemented features. They may be listed as future work.

## 17. Validation commands

Before handoff, run:

```bash
uv lock --check
uv --directory archive-magic-fetch run pytest
uv --directory archive-magic-navigator run pytest
uv run --package archive-magic-fetch archive-magic-fetch --help
uv run --package archive-magic-navigator archive-magic-navigator --help
git diff --check
git status --short
```

Also run the real local smoke test when the untracked/ignored `archives/`
fixture is present.

Report:

- exact commands;
- pass/fail counts;
- skipped integration tests and why;
- manual browser checks performed;
- any changes to `uv.lock`; and
- any remaining risks.

Do not claim that browser replay works based only on unit tests or a successful
process start.

## 18. Recommended implementation sequence

Keep changes reviewable in this order:

1. **Spike:** Prove pinned pywb against the real existing archive and save
   `PYWB-SPIKE.md`.
2. **Scaffold:** Add workspace/package metadata, MIT license, README skeleton,
   and CLI help.
3. **Validation:** Implement collection discovery and streaming CDXJ/WARC-path
   preflight with unit tests.
4. **Configuration:** Generate the spike-proven YAML shapes with unit tests.
5. **Lifecycle:** Supervise pywb, detect readiness/failure, handle signals, and
   clean temporary state.
6. **Integration fixture:** Add compact WARC/CDXJ data and real pywb tests.
7. **Branding:** Add the smallest original template/static customizations.
8. **Documentation:** Finish README, limitations, licenses, and smoke-test
   instructions.
9. **Regression:** Run Fetch and Navigator validation together.

Do not begin with templates or a custom frontend. The first risk is pywb/data
compatibility, followed by safe process supervision.

## 19. Definition of done

Phase 2 is done when every item below is true:

- [ ] `archive-magic-navigator` installs as a uv workspace member.
- [ ] The CLI runs independently of the Fetch package.
- [ ] Python is constrained to `>=3.12,<3.13` for Navigator.
- [ ] Pywb is pinned to stable 2.9.1.
- [ ] Navigator invokes pywb only as a separate process.
- [ ] One selected local collection replays.
- [ ] All valid local collections can be selected from the pywb homepage.
- [ ] Multiple captures appear and older/newer navigation works.
- [ ] Response and revisit records replay.
- [ ] Archived subresources are rewritten and loaded.
- [ ] No live-web fallback, recording, proxy, or auto-indexing is enabled.
- [ ] Collection validation rejects unsafe paths and malformed indexes.
- [ ] No file beneath `archives/` changes.
- [ ] Ctrl-C and startup failures leave no child process or temporary state.
- [ ] Light branding does not replace pywb replay controls.
- [ ] Unit and real-pywb integration tests pass.
- [ ] The existing Fetch test suite still passes.
- [ ] README and spike notes accurately describe the implemented behavior.
- [ ] Navigator's MIT license and pywb's separate GPLv3 status are clear.

## 20. Stop-and-escalate conditions

Stop and ask for a design decision rather than expanding scope if:

- valid Fetch output requires WARC or CDXJ mutation for pywb to replay;
- pywb 2.9.1 requires enabling live, recording, proxy, or auto-index behavior;
- serving one collection requires copying it into a private pywb collection;
- the only practical implementation requires importing pywb internals;
- the built-in UI cannot provide capture selection or older/newer navigation;
- branding requires copying substantial GPL-covered pywb templates into the MIT
  package;
- a dependency conflict would require changing Fetch's standalone Python or
  dependency support;
- safe behavior would require implementing concurrent publication or object
  storage now; or
- a requested packaging format bundles pywb and needs a GPL distribution
  decision.

When escalating, provide:

1. the smallest reproduction;
2. exact versions and commands;
3. observed and expected behavior;
4. relevant pywb documentation/source links;
5. the least invasive options; and
6. the effect of each option on the Fetch/Navigator boundary.

## 21. Deferred work that must remain deferred

Do not implement these during Phase 2:

- safe concurrent Fetch publication;
- generation manifests or garbage collection;
- S3 or R2 credentials, indexing, or range-read adapters;
- local CDXJ caches for remote collections;
- ZipNum or sharded indexes;
- public internet deployment;
- TLS, authentication, authorization, or multi-user state;
- preferences, recent pages, or last-page-viewed;
- WACZ;
- React or another custom frontend;
- production observability; or
- automatic archive repair.

Design current code so these remain possible, chiefly by keeping the local
collection/configuration boundary narrow and pywb replaceable.

## 22. Future invariants to preserve

Although not implemented now, do not make choices that contradict these future
requirements:

- WARC objects must remain byte-range addressable by CDXJ offset and length.
- Remote WARC objects should be immutable and generation-qualified.
- A future manifest should atomically select a complete generation.
- Navigator should pin one generation for the duration of a request/worker
  view.
- Remote credentials must remain outside collection metadata.
- S3/R2 acceptance tests must prove `206 Partial Content` and transferred byte
  counts.
- Fetch and Navigator must continue to install and run independently.

## 23. Upstream references

- [pywb 2.9.1 release](https://github.com/webrecorder/pywb/releases/tag/v-2.9.1)
- [pywb usage](https://pywb.readthedocs.io/en/latest/manual/usage.html)
- [pywb command-line applications](https://pywb.readthedocs.io/en/latest/manual/apps.html)
- [pywb configuration](https://pywb.readthedocs.io/en/latest/manual/configuring.html)
- [pywb template guide](https://pywb.readthedocs.io/en/latest/manual/template-guide.html)
- [Pinned pywb CLI source](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/apps/cli.py)
- [Pinned pywb WarcServer source](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/warcserver/warcserver.py)
- [Pinned local/HTTP/S3 loaders](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/utils/loaders.py)
- [Pinned block record loader](https://github.com/webrecorder/pywb/blob/v-2.9.1/pywb/warcserver/resource/blockrecordloader.py)
- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/)
- [GNU GPL FAQ](https://www.gnu.org/licenses/gpl-faq.html)
- [MIT license](https://opensource.org/license/mit)
