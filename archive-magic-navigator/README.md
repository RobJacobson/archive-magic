# Archive Magic Navigator

Archive Magic Navigator opens WARC/CDXJ collections produced by Archive Magic
Fetch in pywb's browser viewer. It validates local collections read-only and,
by default, loads resources missing locally from the Internet Archive's
Wayback Machine. Navigator generates temporary configuration outside the
archive tree and runs the pinned pywb server as a separate child process.

## Install

Navigator currently requires ordinary CPython 3.12:

```bash
uv sync --package archive-magic-navigator
```

The CLI package is MIT licensed. It depends at runtime on
[pywb 2.9.1](https://github.com/webrecorder/pywb/tree/v-2.9.1), which is a
separate GPLv3-licensed distribution. Navigator does not vendor pywb or import
its implementation modules.

## Run

Serve one collection:

```bash
uv run --package archive-magic-navigator \
  archive-magic-navigator wecanstopthehate.org
```

Serve every immediate collection directory:

```bash
uv run --package archive-magic-navigator \
  archive-magic-navigator --all
```

Use `--archives PATH` for another collections root, `--port PORT` for another
port, and `--open` to open the landing page after the server is ready. Wayback
fallback is on by default; use `--wayback-fallback off` for strictly local
replay.

Navigator binds to `127.0.0.1:8080` by default. An explicit non-loopback
`--bind` exposes an unauthenticated development server without TLS or
internet-facing hardening. Archived pages can contain hostile or obsolete
scripts even when served locally.

Wayback fallback requires an internet connection. Local captures always take
precedence. When a requested page, redirect target, or asset is not stored
locally, pywb asks the Wayback Machine for the capture nearest the replay
timestamp. Remote lookup and loading use a ten-second timeout. Navigator does
not cache or persist fallback responses.

## Collection contract

Each collection must contain:

```text
<collection>/
├── archive/
│   └── **/*.warc.gz
└── replay/
    └── index.cdxj
```

CDXJ `filename` values must be safe collection-relative `archive/...` paths.
Navigator reads indexed compressed byte ranges directly and never copies,
repairs, reindexes, or records collection data. It never writes Wayback
fallback responses into the collection. There is no fallback to the current
live web.

Collection directory names also become browser routes. They must start with an
ASCII letter or digit, contain only ASCII letters, digits, `.`, `_`, or `-`,
and must not use Navigator-reserved names such as `static`.

Running Navigator while Fetch is publishing the same collection is unsupported.
Stop Fetch or wait for it to finish before starting Navigator.

## Development

From the repository root:

```bash
uv lock --check
uv --directory archive-magic-fetch run pytest
uv --directory archive-magic-navigator run pytest
```

The ignored local `archives/wecanstopthehate.org` collection is useful for a
manual smoke test but is not required by deterministic CI.
