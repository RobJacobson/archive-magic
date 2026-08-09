# Flat Portable Collections Refactor

## Summary

Refactor Fetch to produce flat, independently replayable collection directories and Navigator to aggregate those collection-level indexes directly through pywb.

```text
archives/<domain>/
├── collections/
│   └── <collection-id>/
│       ├── <domain>-<collection-id>-001.warc.gz
│       └── <domain>-<collection-id>-index.cdxj
└── captures/
    └── <collection-id>/
        └── runs/
            └── <run-id>/
                ├── run.json
                └── page-001.cdx.gz
```

No root index, catalog, collection manifest, failure ledger, or legacy-layout compatibility will remain.

## Implementation Changes

### Archive Magic Fetch

- Introduce domain/archive and portable-collection layout abstractions. Collection IDs use the existing safe filename character policy; the current CLI continues creating four-digit year IDs while acquisition and publication code accepts generic IDs.
- Write WARC shards and one index directly under `collections/<collection-id>/`. Name indexes `<domain>-<collection-id>-index.cdxj` and store WARC basenames—not parent-relative paths—in each CDXJ `filename`.
- Preserve collection-local invariants: immutable finalized WARCs, size-bounded shards, collection-only revisit references, sorted CDXJ output, range validation, crash reconciliation, and atomic index replacement.
- Remove the collection-wide index merge, `collection.json`, `failures.json`, persistent failure loading, and the old `archive/`, `sources/`, and root `.work` layout. Put temporary work beneath `captures/.work/`.
- Allocate one invocation run ID and use it under every selected collection. Save raw CDX pages consistently as `page-NNN.cdx.gz`.
- Atomically write `run.json` last for each normally completed collection, making its presence the completion marker. It contains schema version, run/archive/collection IDs, URL pattern and bounded dates, query metadata, counts, timings, failures from that run, and a full post-run WARC/index artifact snapshot with sizes, hashes, and record counts.
- Do not carry failures between runs. A rerun requeries CDX, inventories existing WARCs, skips represented captures, and retries missing captures; earlier outcomes remain in immutable run records.
- Create `captures/<id>/` even when a query produces no playable records, but create `collections/<id>/` only when at least one finalized WARC and nonempty index exist.
- Detect legacy root artifacts and fail with a concise regenerate-required error rather than reading, migrating, or mixing layouts.

### Archive Magic Navigator

- Treat each immediate directory under `--archives` as a domain archive. Rename CLI/user-facing terminology from `COLLECTION` to `ARCHIVE`; `--all` continues to serve every domain archive.
- Discover portable collections from `<domain>/collections/*`, sorted by collection ID. Ignore `<domain>/captures/`; require at least one playable collection.
- Require exactly the expected `<domain>-<collection-id>-index.cdxj` in each collection directory. Validate sorted CDXJ records, basename-only WARC locators, expected WARC naming, containment, regular files, and indexed byte ranges within that same directory.
- Generate one pywb route per domain using an `index_group` containing every collection index and an ordered `archive_paths` list containing every flat collection directory. Preserve the existing local-first Internet Archive fallback sequence.
- Keep WARC names globally unique through the domain/collection prefix so pywb cannot resolve a basename from the wrong archive path.
- Update startup diagnostics to report domain archives and their total portable collection count.

### Documentation and terminology

- Update Fetch architecture, Navigator architecture, and Navigator README to distinguish a domain archive from its portable collections.
- Document that only `<domain>/collections/**` is required for playback or bucket publication; `<domain>/captures/**` is acquisition provenance.
- Document that years are the current grouping strategy, not a filesystem or replay-layer requirement, and that arbitrary grouping is intentionally deferred.

## Test Plan

- Update Fetch layout, naming, raw-CDX, indexing, shard rollover, revisit-closure, reconciliation, atomic publication, and rerun tests for flat collection directories and basename locators.
- Verify `run.json` embeds query metadata and current-run failures, is written last, snapshots all resulting artifacts, and that reruns retry missing captures without a persistent ledger.
- Verify empty years create capture history but no invalid playback collection, while multi-year invocations create independent portable collections sharing the invocation run ID.
- Add rejection tests for legacy layouts, unsafe generic collection IDs, foreign filenames, cross-collection CDXJ paths, and cross-collection revisits.
- Update Navigator discovery and validation fixtures for multiple flat collections under one domain; confirm `captures/` is ignored.
- Verify generated pywb YAML contains one index-group entry and archive path per portable collection, both with and without Wayback fallback.
- Extend the real-pywb integration test to replay captures from two collection directories through one domain route, including same-collection cross-shard revisits.
- Run both package test suites, including Navigator’s pinned pywb integration tests.

## Assumptions

- Existing generated archives have been deleted; no migration or dual-layout support is required.
- The public Fetch CLI remains date-based and year-grouped in this release.
- Navigator performs local filesystem discovery; `catalog.json` and remote bucket loading are deferred.
- Active collections are expected not to contain duplicate capture identities; overlap precedence is deferred until non-year grouping is introduced.
