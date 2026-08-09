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

Serve one domain archive:

```bash
uv run --package archive-magic-navigator \
  archive-magic-navigator wecanstopthehate.org
```

Serve every immediate domain archive directory:

```bash
uv run --package archive-magic-navigator \
  archive-magic-navigator --all
```

Use `--archives PATH` for another archives root, `--port PORT` for another
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

Archive Magic Fetch preserves selected historical redirect responses but does
not automatically capture their targets. Locally captured targets and assets
take precedence exactly like primary records; runtime Wayback fallback can
supply missing resources without changing the local archive.

## Archive and collection contract

Each domain archive contains one or more flat portable collections:

```text
<domain>/
├── collections/
│   ├── 2004/
│   │   ├── example.org-2004-001.warc.gz
│   │   └── example.org-2004-index.cdxj
│   └── 2005/
│       ├── example.org-2005-001.warc.gz
│       └── example.org-2005-index.cdxj
└── captures/                    # ignored by Navigator
    └── 2004/runs/...
```

Each CDXJ `filename` must be the basename of a WARC in the same portable
collection. Navigator validates every collection, supplies its indexes to pywb
as one index group, and supplies the corresponding collection directories as
archive paths. It reads indexed compressed byte ranges directly and never
copies, repairs, reindexes, or records archive data.

Only `<domain>/collections/**` is required for playback or bucket publication.
`<domain>/captures/**` contains Fetch provenance and is ignored. Fetch currently
groups by year, but collection IDs and Navigator discovery are not year-specific.
Revisits may cross WARC shards inside a collection but never depend on another
collection. There is no domain-wide merged index.

Domain archive names become browser routes. They must start with an
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

The ignored local `archives/wecanstopthehate.org` archive is useful for a
manual smoke test but is not required by deterministic CI.
