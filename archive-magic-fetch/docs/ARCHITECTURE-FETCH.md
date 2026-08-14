# Archive Magic Fetch Architecture

## Purpose and boundary

Archive Magic Fetch turns Internet Archive capture history into portable WARC 1.1
collections and CDXJ indexes. It is a standalone producer. It does not call or
coordinate with Navigator; the two applications interact only by reading and
writing the locations described by the same archive descriptor.

One Fetch process on one machine owns an archive prefix. Concurrent writers,
secondary updater machines, and manual bucket mutation are unsupported.

## Public interface

```text
archive-magic-fetch ARCHIVE [--start DATE] [--end DATE] [--reset-data]
```

`ARCHIVE` is either a file named `archive.toml` or its containing directory. There
is no legacy URL-pattern, `--config`, or `--archives-root` interface.

Normal CLI dates narrow a run without changing the descriptor. Without overrides,
Fetch uses `[fetch].start` through `[fetch].end`; an omitted end is resolved to the
current UTC timestamp when the run starts. Fetch therefore checks the complete
configured history on daily or weekly runs. Identity-based deduplication makes
unchanged older collections no-ops, so normally only the current year is
republished.

## Descriptor contract

The descriptor is user-authored intent:

```toml
schema_version = 1

[archive]
id = "example.org"
url_pattern = "*.example.org"

[storage]
authority = "remote" # "local" or "remote"
workspace_directory = "workspace"

[storage.remote]
bucket = "archive-magic"
prefix = "example.org"
endpoint_url = "https://s3.example.invalid"
region = "auto"

[fetch]
start = "1995-01-01"
# end omitted means now
warc_target_bytes = 250000000
playback_workers = 4
playback_starts_per_second = 20.0

[playback]
wayback_fallback = true
```

Rules:

- `schema_version` must be exactly `1`; unknown keys are errors.
- Archive IDs must be route-safe and cannot be `.`, `..`, or `static`.
- Relative paths and `~` are resolved from the descriptor directory.
- `workspace_directory` is the exact archive root. The archive ID is not appended.
- `storage.remote` is required for remote authority and forbidden for local
  authority.
- Bucket credentials come exclusively from Boto3's standard credential chain.
  No adjacent `.env` is loaded and no specific access-key variable is required.
- The compressed WARC rollover target defaults to 250,000,000 bytes and remains
  configurable per archive.

Fetch validates the complete shared descriptor, including playback keys, so Fetch
and Navigator reject the same malformed contract.

## Workspace and publication layout

```text
<workspace_directory>/
  collections-manifest.json
  collections/
    2004/
      example.org-2004-001.warc.gz
      example.org-2004-002.warc.gz
      example.org-2004-index.cdxj
  captures/
    2004/
      runs/<run-id>/run.json
      ... diagnostic acquisition state ...
```

`collections/` is portable playback data. `captures/` records local diagnostics,
queries, and run state; it is deliberately not bucket-authoritative archive
content. `collections-manifest.json` is generated publication state and retains its
existing unversioned JSON shape. It must never be edited as configuration.

Short-lived files use same-directory temporary names followed by atomic local
replacement. A visible `.warc.gz.partial` represents an interrupted open shard and
is salvaged on the next run when possible.

## Acquisition pipeline

For each year in the selected range, Fetch:

1. Materializes the collection. Local authority is a no-op; remote authority
   downloads the stable CDXJ and only the manifest's final WARC.
2. Reconciles interrupted local WARC/index work for that collection.
3. Queries Internet Archive CDX history for the configured URL pattern.
4. Parses and deduplicates captures by canonical capture identity.
5. Inventories existing captures from CDXJ identity metadata and skips those
   already represented.
6. Resolves remaining captures through bounded, rate-limited playback workers.
7. Appends response or revisit records through one serialized WARC writer.
8. Reindexes changed/new WARCs, merges their lines into the stable CDXJ, and
   validates the completed artifacts.
9. Publishes only changed/new WARCs plus the CDXJ, writes an immutable local run
   record, and evicts remote working copies after confirmed success.

Acquisition can be parallel; WARC mutation and publication remain serialized.
Failures for individual captures are recorded without corrupting already committed
records. At the process boundary, exceptions produce a nonzero exit, and the next
run reconciles recoverable partial work.

Payloads are never reused across collections: a capture missing from its current
collection is downloaded from Internet Archive unless CDX metadata can synthesize
an empty response or slash redirect. The `payload-reuses` metric counts only those
CDX-synthesized outcomes.

## Append-only WARC behavior

WARC files use gzip member concatenation. Existing records are immutable. A normal
remote update may change at most the lexicographically final WARC already named by
the manifest, its new length must be greater, and the SHA-256 of its old-length
prefix must equal the manifest digest. Earlier WARC objects cannot change or
disappear.

When the final shard is at or beyond the target, the writer creates the next
sequence. Consequently a normal update transfers one tail WARC plus its updated
CDXJ, or creates one new WARC plus the CDXJ. All other WARC objects remain
untouched.

The stable CDXJ contains the original CDX digest, status token, and URL key for
every response and revisit. Fetch can therefore reconstruct exact capture
inventory without rescanning WARCs. Incremental indexing removes prior lines for
changed filenames, adds their replacement lines, sorts the result, validates byte
ranges against manifest sizes, and trusts the previously committed CDXJ's semantic
relationships. Archives created without the required identity fields must be reset
and regenerated.

## Local authority

With `authority = "local"`, the workspace is authoritative. Fetch publishes WARC,
CDXJ, and manifest files using local atomic replacement. Navigator may serve the
same workspace directly. Normal publication preserves all previous collections;
`--reset-data` retains selected-collection reset behavior.

## Remote authority and bounded materialization

With `authority = "remote"`, the bucket prefix and its manifest are authoritative.
The workspace is a bounded working area and the recovery source for uncommitted
work. At startup Fetch reads the manifest. For each selected collection it then:

1. Downloads the committed CDXJ unless a validated copy is present.
2. Downloads only the manifest's final WARC, preserving a valid local exact-prefix
   extension recovered from interrupted work.
3. Runs the same local append and incremental-index code used by local authority.
4. Publishes explicit changed/new artifacts and commits the manifest last.
5. Writes the run record, then deletes that collection's finalized local
   WARC/CDXJ files.

Partials, `captures/`, run diagnostics, and the small cached manifest remain local.
Any publication failure retains materialized WARC/CDXJ files for recovery. Missing
local artifacts are never interpreted as remote deletions.

An archive prefix must either be empty, contain a valid manifest, or have valid
local WARC/CDXJ work from an interrupted initial publication. A nonempty prefix
without a manifest or matching local recovery files stops rather than guessing.

## Remote publication and recovery

After local WARC and CDXJ validation, one changed collection is published in this
order:

1. Upload the extended tail or newly rolled WARC. Unchanged WARC objects are
   skipped.
2. Replace the live CDXJ. A single S3 key is atomic, so readers see
   either all old bytes or all new bytes.
3. Replace `collections-manifest.json` last and re-read it for verification. This
   is the commit point.
4. Write the run record, then evict finalized local working files.

If a write fails, Fetch stops and retains the workspace. On the next run it uses
the committed manifest plus the retained exact-prefix tail/new rollovers and CDXJ
to reconstruct the intended collection through the normal incremental-index and
publication path. If the workspace was lost after a partial same-key publication,
automatic recovery is impossible and the archive must be reset and regenerated.

There is intentionally a short window after CDXJ replacement and before manifest
commit in which a fresh remote reader may reject the archive. Cached Navigator
instances continue with their last validated index; the next Fetch run completes
forward recovery. No versioned index keys or reader fallback protocol are added.

## Destructive reset

Remote `--reset-data` is explicit authorization for whole-archive maintenance. It:

- rejects `--start` and `--end` overrides;
- prints a prominent deletion/playback-downtime warning;
- deletes only the descriptor's complete configured bucket prefix;
- clears only the exact workspace archive root; and
- rebuilds the descriptor's full configured range.

There is no interactive prompt. The old manifest is absent during the rebuild, so
remote playback is unavailable until publication completes. Other prefixes in the
same bucket are not touched.

## Module map

- `config.py`: descriptor path resolution and strict schema validation.
- `cli.py`: public argument contract and exit-code boundary.
- `fetch.py`: configured-history orchestration and yearly lifecycle.
- `collection.py`: exact workspace paths, atomic files, and run records.
- `warc.py`: append-only WARC writer, rollover, and partial salvage.
- `index.py`: deterministic full or incremental collection CDXJ generation.
- `inventory.py`: CDXJ-driven identity inventory and within-collection revisits.
- `storage.py`: local manifests plus remote materialization, publication,
  workspace-backed recovery, and eviction.
- `cdx.py`, `resolution.py`, `workers.py`, `playback.py`: capture discovery and
  bounded playback acquisition.

## Verification

The Fetch test suite is run independently from Navigator:

```console
uv run pytest -q
```

Coverage includes descriptor conformance, the 250 MB default and configurable
rollover, exact-prefix continuation, tail-only materialization, incremental
indexing, publication order, workspace-backed recovery at each upload boundary,
successful eviction, failure retention, unchanged-artifact suppression, and reset
prefix scope.
