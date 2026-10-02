# Archive Magic Navigator architecture

## Boundary and inputs

Navigator serves bucket-backed catalogs independently of Fetch. `--catalog`
requires a JSON file; legacy TOML and directory catalogs are rejected with migration
instructions. Runtime flags control binding, cache, polling, fallback, and debugging.
One catalog entry serves one website. Local-source CLI playback is retired; local
index helpers remain internally covered for replay-format regression testing.

`settings.py` validates the unversioned catalog and bucket manifest contracts.
The catalog contains title, shared storage endpoint/region, and ordered bucket/
prefix locations. Credentials remain in the standard AWS environment/profile.
Metadata and images come from the bucket's archive.json and relative object keys.
The manifest ID must match existing WARC/CDXJ names. See repository examples.

An archive root contains archive.json, assets/, and data/. All replay files are
flat under data/. Root-level WARC/CDXJ objects trigger an explicit migration error,
never implicit copying/deletion. Navigator never writes to remote storage.

## Loading, cache, and publication

`catalog.py` owns per-location states and a sequential background refresh loop.
Each entry independently downloads/validates its manifest, loads images, and
initializes or refreshes `RemoteArchiveStore`. Failed entries remain in the catalog
and retry. Duplicate IDs at startup are fatal; recovery conflicts cannot replace
an already claimed archive. Accepted IDs are fixed for the process lifetime.

Cache directories are keyed by SHA-256 of endpoint, region, bucket, and prefix;
index subdirectories additionally use archive ID. Manifest/image cache records
atomically store both ETag and encoded object bytes. Conditional GET avoids
unchanged downloads; invalid new metadata/images never replace accepted copies.
Images are limited to 8 MiB and identified as PNG/JPEG/WebP/GIF; manifests are
limited to 1 MiB. Assets are published to immutable content-hashed runtime paths.

Remote index loading lists data/, conditionally downloads CDXJ against listed
ETags, checks size and optional SHA-256 metadata, and validates ordering, timestamps,
WARC names and byte bounds. Changed annual indexes are staged; a merged replay
snapshot is atomically replaced. Missing annual indexes are retained during a
running session. Failed publication preserves the last merged snapshot. A cached
index cannot protect WARC bytes that are deleted or destructively rewritten.

Only indexes, metadata, and images are cached. The generated pywb archive path is
s3://bucket/prefix/data/ and payloads are fetched by authenticated range requests.
Index changes require a merged-index rewrite; disk requirements include annual
indexes, a merged copy, and temporary publication copies. Capture summaries are
recomputed only when the merged snapshot or entry-point metadata changes.

## Frontend and lifecycle

The parent stages templates/static files and supervises a separate Python process
running Navigator's pywb adapter, pinned to pywb 2.9.1. Readiness uses the existing
private marker protocol; signal forwarding and bounded logs remain in place.
The parent publishes catalog-state.json atomically after refresh. The child reads
changed snapshots on requests, registers newly recovered pywb collections, and
renders the homepage from accepted state. Existing collection index paths stay
fixed. No recording, automatic acquisition, or live-web source is enabled.

Cards retain catalog order, show bucket metadata and derived capture dates, and
link to the nearest featured or latest homepage capture. Nearest ties prefer the
earlier timestamp. Missing entry captures link to archive search. Presentation
text is escaped, asset paths are contained, and keyboard focus is visible.
Wayback fallback remains a process-wide option (on by default), independent of
selection of catalog entry captures.

A bucket outage does not stop other entries. Cached data is marked stale;
uncached entries show unavailable. Missing images use placeholders. Server catalog
edits and archive identity changes require restart. The frontend remains a local
convenience server without production authentication, TLS, or content isolation.

## Verification

Unit tests cover contracts, paths, identities, cache separation, selection, refresh,
failures and recovery. Real-pywb tests cover private multi-bucket cold starts,
metadata/image updates, late route registration, replay ranges, and prior replay
behavior. Fetch tests separately exercise publication/reset preservation with real
rclone against temporary local directories. No tests mutate real buckets.
