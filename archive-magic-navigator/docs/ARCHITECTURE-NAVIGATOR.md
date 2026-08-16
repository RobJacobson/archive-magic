# Archive Magic Navigator Architecture

## Purpose and boundary

Archive Magic Navigator is a standalone playback process. It validates published
Archive Magic collections, generates an isolated pywb runtime configuration, and
serves replay UI/routes. It never asks Fetch to acquire data and has no control
channel with Fetch. Both applications merely interpret the same `archive.toml` and
storage layout.

Navigator does not mutate WARC or user-authored archive configuration. For remote
playback it writes only validated CDXJ/manifest cache files.

## Public interface

Serve one descriptor or its containing directory:

```text
archive-magic-navigator ARCHIVE
  [--source {auto,local,remote}]
  [--cache PATH]
  [--poll-interval SECONDS]
  [--bind ADDRESS] [--port PORT]
  [--wayback-fallback {on,off}]
  [--open] [--debug]
```

Serve a descriptor catalog:

```text
archive-magic-navigator --catalog PATH
  [--source {auto,local,remote}]
  [--cache PATH]
  [--poll-interval SECONDS]
  [--bind ADDRESS] [--port PORT]
  [--wayback-fallback {on,off}]
  [--open] [--debug]
```

Exactly one of `ARCHIVE` and `--catalog` is required. The legacy archive-ID,
`--all`, `--archives`, and component-config interfaces do not exist.

Process settings remain on the CLI:

- `--bind` defaults to `127.0.0.1` and `--port` to `8080`.
- `--poll-interval` defaults to 60 seconds and must be positive.
- `--open` opens a browser only after pywb is ready.
- `--debug` is passed to pywb.
- `--wayback-fallback` globally overrides descriptor playback policy. Without it,
  each archive keeps its own `[playback].wayback_fallback` value.

Non-loopback binds print a warning because this is an unauthenticated development
replay server, not a hardened public hosting layer.

## Shared descriptor

Navigator consumes the same strict schema as Fetch:

```toml
schema_version = 1

[archive]
id = "example.org"
url_pattern = "*.example.org"

[storage]
authority = "remote"
data_directory = "data"

[storage.remote]
bucket = "archive-magic"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"

[fetch]
start = "1995-01-01"
warc_target_bytes = 250000000

[playback]
wayback_fallback = true
```

`archive.toml` is user-authored intent. `collections-manifest.json` is generated,
versioned publication state.

Unknown keys and unsupported schema versions fail startup. The archive ID is
validated for safe use in pywb routes and local paths. Relative paths and `~` are
resolved from the descriptor directory. `data_directory` is the exact archive
data root; Navigator never appends the archive ID. Remote authority requires
`[storage.remote]`, while local authority rejects it.

Navigator validates the shared Fetch table even though it does not use acquisition
settings. This keeps descriptor conformance consistent between the standalone
packages.

## Catalog discovery

Catalog mode scans only immediate, non-hidden children:

```text
<catalog>/
  example.org/archive.toml
  example.net/archive.toml
  .ignored/archive.toml
```

It does not recursively search and does not treat a descriptor directly in the
catalog root as an entry. Paths are sorted by child directory name for deterministic
route/config generation. Startup aggregates descriptor errors and fails for any
invalid entry or duplicate archive ID; it never silently serves a partial catalog.

## Source selection

`--source` applies to every selected descriptor:

- `auto`: local authority uses the exact data directory; remote authority uses its
  bucket.
- `local`: use every descriptor's data directory. A remote-authoritative Fetch
  data directory normally has no finalized WARC/CDXJ working copies after success, so
  use `remote` or `auto` for those descriptors unless the data directory was populated
  independently.
- `remote`: require remote settings for every entry and use each bucket/prefix.

Mixed local and remote catalog playback is supported under `auto`. For all entries
selected as remote, `endpoint_url` and `region` must be identical because pywb is
launched with one S3 process environment. Buckets and prefixes may differ. Process
credentials must also be compatible: Boto3 and pywb use one standard credential
environment, and Archive Magic neither injects per-archive keys nor loads `.env`.

## Local playback

A local archive has this exact root:

```text
<data_directory>/
  collections-manifest.json
  example.org-2004-001.warc.gz
  example.org-2004-index.cdxj
  example.org-2005-001.warc.gz
  example.org-2005-index.cdxj
```

Navigator discovers logical yearly collections from the strict index filenames,
validates their index/WARC structure, and gives pywb the shared data path. Fetch
run records live in the sibling `logs/` directory and are never exposed as archive
content.

## Remote playback and visible cache

Remote mode creates one `RemoteArchiveStore` per descriptor. Its default cache is:

- `<descriptor-directory>/navigator-cache/` for one archive;
- `<catalog>/navigator-cache/` for catalog mode; or
- the exact path supplied by `--cache`.

Within it, archive IDs remain separated:

```text
navigator-cache/
  example.org/
    collections-manifest.json
    example.org-2004-index.cdxj
```

WARC objects are not downloaded into this cache. The generated pywb collection uses
an authenticated `s3://bucket/prefix/` archive path, allowing pywb
to issue byte-range reads using the standard AWS credential chain. Only indexes and
the last accepted manifest are cached locally.

At startup the remote store:

1. Reads `collections-manifest.json` and records its ETag.
2. Strictly validates its shape, timestamps, IDs, keys, ETags, SHA-256 values, and
   sizes.
3. Downloads each CDXJ with the index artifact ETag as `If-Match`.
4. Verifies size and SHA-256 and validates every CDXJ WARC filename/range against
   the manifest's WARC inventory.
5. Stages all required index files beside their destinations and atomically replaces
   them only after validation.
6. Atomically caches the accepted manifest.

If remote startup fails, Navigator may use the previous cache only when its
manifest and every cached index still pass the same validation. Otherwise startup
fails.

## Polling and publication continuity

Each remote store polls the manifest ETag at the configured interval. An unchanged
ETag is a no-op. For a changed manifest, Navigator stages and validates changed
indexes before atomically replacing each cached index and advancing its accepted
manifest state.

Fetch publishes WARC objects, then the index, then the manifest. This order makes
the manifest the visibility boundary:

- Before manifest commit, Navigator sees the old manifest and keeps the old index.
- After commit, all referenced artifacts should be complete.
- If the new manifest/index read is temporarily inconsistent, preconditioned,
  corrupt, or unavailable, polling logs a warning, retains the previous validated
  cache/state, and retries later.
- An extended WARC is safe with an old index because all old byte ranges are
  unchanged.

Collection membership changes require a Navigator restart. Polling warns and keeps
the currently configured route set; it hot-adopts index changes for collections
that already exist.

## Per-archive Wayback fallback

The generated pywb configuration assigns each route its effective policy. A catalog
can therefore contain both fallback-enabled and fallback-disabled archives. The CLI
override forces all routes on or off for one process invocation.

Fallback behavior is a playback policy only. It does not alter stored WARC data,
the descriptor, or the publication manifest.

## Generated pywb runtime

Navigator creates a temporary runtime directory for each process invocation and
writes generated pywb YAML there. The configuration maps route-safe archive IDs to
their validated CDXJ and local or S3 archive locations, installs Archive Magic UI
templates/static resources, and applies effective fallback policies. The temporary
runtime is removed when the process exits; user data and the visible remote cache
remain.

The process waits for the child server readiness signal before printing its URL or
opening a browser. Signals and normal shutdown stop remote polling and terminate the
child cleanly.

## Failure and trust model

Navigator trusts only descriptor-validated paths plus manifest-validated artifact
metadata. It rejects traversal, reserved route names, duplicate IDs, malformed CDXJ
rows, unknown manifest keys, out-of-bounds WARC ranges, and changed artifacts that
do not match their declared digest.

Expected transient conditions—an in-progress publication, conditional read
failure, provider outage, or malformed new manifest—do not destroy the last known
good cache. Unexpected startup conditions without a valid cache are fatal and
produce a nonzero exit.

Archive replay can contain hostile historical content. Navigator provides local
convenience, not authentication, TLS termination, content isolation guarantees, or
multi-tenant hardening.

## Module map

- `settings.py`: strict descriptor loading and catalog discovery.
- `cli.py`: public arguments, source selection, aggregate validation, and process
  lifecycle.
- `collections.py`: local exact-root discovery and route-safe collection models.
- `remote.py`: shared manifest types, atomic cache adoption, polling, and S3 paths.
- `validation.py`: playable archive/CDXJ checks.
- `config.py`: per-archive pywb and fallback configuration generation.
- `process.py`: child process readiness, loopback checks, and shutdown.
- `templates/`, `static/`: Navigator replay UI resources.

## Verification

Run Navigator separately from Fetch because both repositories contain a
`test_config.py` module name:

```console
uv run pytest -q -m 'not integration'
```

The loopback/real-pywb integration suite additionally requires an environment that
permits local socket binding:

```console
uv run pytest -q -m integration
```

Tests cover descriptor and CLI cutover, deterministic catalogs, source overrides,
remote environment compatibility, per-archive fallback generation, cached playback
during manifest/index mismatch, atomic polling adoption, authenticated S3 archive
paths, and local pywb startup.
