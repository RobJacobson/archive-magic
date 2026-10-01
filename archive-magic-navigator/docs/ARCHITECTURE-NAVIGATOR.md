# Archive Magic Navigator Architecture

## Purpose and boundary

Archive Magic Navigator is a standalone playback process. It validates published
Archive Magic collections, generates an isolated pywb runtime configuration, and
serves replay UI/routes. It never asks Fetch to acquire data and has no control
channel with Fetch. The two applications interact only through the archived
WARC/CDXJ layout in a flat directory.

Navigator does not mutate WARC or user-authored archive configuration. For both source types
it writes only CDXJ cache files.

## Public interface

Serve one configuration file or its containing directory:

```text
archive-magic-navigator ARCHIVE
  [--cache PATH]
  [--poll-interval SECONDS]
  [--bind ADDRESS] [--port PORT]
  [--wayback-fallback {on,off}]
  [--open] [--debug]
```

Serve a configuration catalog:

```text
archive-magic-navigator --catalog PATH
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
- `--poll-interval` defaults to 300 seconds for both source types and must be finite and positive.
- `--open` opens a browser only after pywb is ready.
- `--debug` is passed to pywb.
- `--wayback-fallback` defaults to `on` for the whole process, including catalogs.

Non-loopback binds print a warning because this is an unauthenticated development
replay server, not a hardened public hosting layer.

## Configuration contract

Navigator reads its own strict `navigator.toml` (or an explicit TOML file of any
name). It does not read Fetch configuration.

```toml
[archive]
id = "example.org"

[source]
type = "local"
directory = "data"
```

Remote source fields are flattened into `[source]`:

```toml
[source]
type = "remote"
bucket = "archive-magic"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"
```

`navigator.toml` is user-authored intent.

The format is unversioned but strict: unknown tables and keys fail startup. The
archive ID is validated for safe use in pywb routes and local paths. Relative
paths and `~` are resolved from the containing TOML file. `directory` is the exact
archive data root; Navigator never appends the archive ID. A remote source
requires `bucket` and rejects `directory`. A local source rejects remote fields.

Each configuration selects exactly one source. There is no process-wide source
override.

## Catalog discovery

Catalog mode scans only immediate, non-hidden children:

```text
<catalog>/
  example.org/navigator.toml
  example.net/navigator.toml
  .ignored/navigator.toml
```

It does not recursively search and does not treat a configuration directly in the
catalog root as an entry. Paths are sorted by child directory name for deterministic
route/config generation. Startup aggregates configuration errors and fails for any
invalid entry or duplicate archive ID; it never silently serves a partial catalog.

Mixed local and remote catalog playback is supported because each file names its
own source. For all remote entries, `endpoint_url` and `region` must be identical
because pywb is launched with one S3 process environment. Buckets and prefixes may
differ. Process credentials must also be compatible: Boto3 and pywb use one
standard credential environment, and Archive Magic neither injects per-archive keys
nor loads `.env`.

## Local playback

A local archive has this exact root:

```text
<source.directory>/
  example.org-2004-001.warc.gz
  example.org-2004-index.cdxj
  example.org-2005-001.warc.gz
  example.org-2005-index.cdxj
```

Navigator discovers logical yearly collections from the strict index filenames,
copies and validates their CDXJ indexes in `navigator-cache/<archive-id>/local/`,
and gives pywb the shared WARC data path. File identity, size, modification time,
and change time detect index changes. A copy is rejected if these attributes change
while it is copied/validated. Source files are never modified. Fetch run records live in the sibling `logs/` directory and are never exposed as archive
content.

## Remote playback and visible cache

Remote mode creates one `RemoteArchiveStore` per remote configuration. Its default
cache is:

- `<configuration-directory>/navigator-cache/` for one archive;
- `<catalog>/navigator-cache/` for catalog mode; or
- the exact path supplied by `--cache`.

Within it, archive IDs remain separated:

```text
navigator-cache/
  example.org/
    example.org-2004-index.cdxj
    .replay.cdxj
```

WARC objects are not downloaded into this cache. The generated pywb collection uses
an authenticated `s3://bucket/prefix/` archive path, allowing pywb
to issue byte-range reads using the standard AWS credential chain. Only indexes
are cached locally.

At startup the remote store:

1. Lists the bucket prefix and discovers collections from strict index filenames.
2. Downloads each CDXJ with the listed object ETag as `If-Match`.
3. Verifies size and optional object metadata SHA-256 and validates every CDXJ WARC
   filename/range against listed WARC sizes.
4. Stages all required index files beside their destinations and atomically replaces
   them only after validation.

If remote startup fails, Navigator validates the cached annual indexes and reuses
the last published merged replay snapshot. This matters when a failed refresh
advanced annual cache files but did not publish the merged snapshot. A cache
created before merged indexes existed is merged after annual validation. Invalid
cache files cause startup to fail.

## Polling and publication continuity

Each store checks its source every five minutes by default (`--poll-interval`
overrides this). Remote stores relist the prefix and download only new or changed
indexes, comparing object ETags and sizes. Local stores rescan the exact source
directory and copy only changed indexes. New annual collections are adopted along
with existing-index updates. A missing annual index does not evict captures during
the running session. Archive routes and TOML configuration remain fixed at startup.

All changed indexes are staged and validated first. Sorted annual CDXJ streams are
merged into a temporary `.replay.cdxj`, the annual cache copies are replaced, and
only then is the merged replay snapshot atomically replaced. pywb uses this fixed
snapshot path, so there is no server restart or configuration reload. Already-open
readers retain their previous snapshot. An unchanged poll does not rewrite it.
If any step fails, playback stays on the previous merged snapshot, accepted source
state is not advanced, a warning is logged, and the next poll retries. Annual cache
files may have advanced individually after a publication failure, but every such
file has passed validation and the replay snapshot remains unchanged.

This keeps Boto3's existing conditional GET and validation workflow; rclone would
add a dependency without replacing the need for validation and atomic adoption.
Only CDXJ data is copied. The merge streams rows rather than loading all indexes
into memory. Each changed batch rewrites the combined index, costing disk I/O
proportional to the archive's total index size and one additional full index copy
(plus a temporary copy during publication).

Fetch publishes WARCs first and replaces the CDXJ last. Local publishers should
likewise atomically replace indexes once referenced WARC ranges are available.
Extended WARCs remain safe with the old index when old byte ranges are unchanged.
Snapshots cannot preserve payloads that are deleted or destructively rewritten
at the source. No acquisition or portable WARC/CDXJ format changes are needed.

## Wayback fallback

`--wayback-fallback on` or `off` applies to every selected archive for that
process. It does not alter stored WARC data or `navigator.toml`.

## Generated pywb runtime

Navigator creates a temporary runtime directory for each process invocation and
writes generated pywb YAML there. The configuration maps route-safe archive IDs to
their validated CDXJ and local or S3 archive locations, installs Archive Magic UI
templates/static resources, and applies effective fallback policies. The temporary
runtime is removed when the process exits; user data and the visible index cache
remain.

The process waits for the child server readiness signal before printing its URL or
opening a browser. Signals and normal shutdown stop index polling and terminate the
child cleanly.

## Failure and trust model

Navigator trusts only configuration-validated paths plus CDXJ structure and, when
a prefix listing is available, WARC range bounds derived from listed object sizes.
It rejects traversal, reserved route names, duplicate IDs, malformed CDXJ rows,
unknown WARC filenames, and out-of-bounds ranges.

Expected transient conditions—an in-progress publication, conditional read
failure, provider outage, or malformed new index—do not destroy the last known
good cache. Unexpected startup conditions without a valid cache are fatal and
produce a nonzero exit.

Archive replay can contain hostile historical content. Navigator provides local
convenience, not authentication, TLS termination, content isolation guarantees, or
multi-tenant hardening.

## Module map

- `settings.py`: Navigator-local TOML loading, path resolution, safety checks, and
  catalog discovery.
- `cli.py`: public arguments, source dispatch, aggregate validation, and process
  lifecycle.
- `collections.py`: local exact-root discovery and route-safe collection models.
- `remote.py`: remote discovery, conditional downloads, validation, and S3 paths.
- `local.py`: local discovery and validated index copying.
- `store.py`: shared polling lifecycle and atomic merged snapshot publication.
- `validation.py`: playable archive/CDXJ checks.
- `config.py`: per-archive pywb and fallback configuration generation.
- `process.py`: child process readiness, loopback checks, and shutdown.
- `templates/`, `static/`: Navigator replay UI resources.

## Verification

Run Navigator separately from Fetch so each application's dependencies and test
boundary are exercised independently:

```console
uv run pytest -q -m 'not integration'
```

The loopback/real-pywb integration suite additionally requires an environment that
permits local socket binding:

```console
uv run pytest -q -m integration
```

Tests cover configuration and CLI cutover, deterministic catalogs, remote
environment compatibility, process-wide fallback generation, cached playback
during index mismatch, failed refresh and recovery, new-year adoption, authenticated
S3 archive paths, and real pywb playback before and after local/remote refreshes.
