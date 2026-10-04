> Historical design plan. Local workspace/configuration/cache locations below were superseded by the [current annotated workspace layout](../README.md#user-workspace) and [migration guide](BUCKET-CATALOG-MIGRATION.md).

# Support multiple archive buckets in Navigator

## Summary

Create a standalone Navigator server whose homepage lists archived websites using metadata and images stored in each archive’s bucket. Navigator reads a single `catalog.json` and requires no access to Fetch’s host or working files.

Create branch `support-multiple-buckets` from `main` when implementation begins, and save this plan on that branch.

Remove `navigator.toml` and its associated configuration interfaces. Use JSON without schema-version fields.

## Layout and configuration

The Fetch host retains this per-website layout:

```text
website-name/
    fetch.toml
    archive.json
    assets/
        logo.png
        preview.jpg
    data/
        <archive-id>-2008-001.warc.gz
        <archive-id>-2008-index.cdxj
    logs/
```

The published bucket—or configured prefix within a bucket—contains:

```text
archive.json
assets/
    logo.png
    preview.jpg
data/
    <archive-id>-2008-001.warc.gz
    <archive-id>-2008-index.cdxj
```

`fetch.toml` remains at the website root. Logs remain in their existing local location. Neither configuration files nor logs are uploaded.

Each bucket’s `archive.json` describes the website:

```json
{
  "id": "wecanstopthehate.org",
  "name": "We Can Stop the Hate",
  "description": "An archive of the organization's website.",
  "homepage": "https://wecanstopthehate.org/",
  "logo": {
    "src": "assets/logo.png",
    "alt": "We Can Stop the Hate"
  },
  "preview": {
    "src": "assets/preview.jpg",
    "alt": "The archived website's homepage"
  },
  "featured_capture": {
    "url": "https://wecanstopthehate.org/",
    "timestamp": "20120615000000"
  }
}
```

The example ID and timestamp are illustrative. Require `id`, `name`, and `homepage`; other fields are optional. IDs must match archive filenames and pass route validation. Image objects require `src` and `alt`; sources are relative to the archive root and cannot reference external URLs or escape that root.

The Navigator server holds its own `catalog.json`:

```json
{
  "title": "Website Archive",
  "storage": {
    "endpoint_url": "https://ACCOUNT_ID.r2.cloudflarestorage.com",
    "region": "auto"
  },
  "archives": [
    { "bucket": "wecanstopthehate-org" },
    { "bucket": "another-website", "prefix": "archive" }
  ]
}
```

- Catalog entries determine inclusion and display order. `prefix` defaults to empty.
- Endpoint and region are shared. Standard AWS endpoint resolution applies when no endpoint is supplied.
- Credentials remain outside JSON in the existing credential environment.
- The catalog contains storage locations, not copies or overrides of website metadata.
- Start Navigator with `archive-magic-navigator --catalog /path/to/catalog.json`. A one-entry catalog serves one website.
- Remove positional archive configuration, directory-catalog discovery, and `navigator.toml` loading. Provide migration errors for obsolete invocation forms.
- Retain existing process flags as runtime controls; introduce no second persisted Navigator configuration.

## Implementation behavior

### Publication and ownership

Treat local `archive.json` and assets as authoring copies. Their explicitly published bucket copies are authoritative for Navigator. Document manual metadata/image publication for this release; ordinary archive-data synchronization must not overwrite or delete them.

Change Fetch’s remote destination to `<bucket>/<prefix>/data/`, with `prefix` identifying the archive root. Preserve existing WARC/CDXJ filenames and local data-directory behavior.

Change remote reset to delete only this archive’s managed WARC/CDXJ files within `data/`. Preserve metadata, assets, unrelated objects, and other archives. Never purge the archive root.

### Remote loading and cache

Load `archive.json` from each configured archive root and discover indexes under its `data/` directory. Reuse existing index validation, atomic merged-index publication, and authenticated WARC range reads.

Cache manifests, images, and indexes, never complete WARC collections. Default to `navigator-cache/` beside the catalog, retaining the cache-location override. Bind cache identity to endpoint, region, bucket, prefix, and archive ID.

Validate JSON fields and asset paths with actionable errors. Support PNG, JPEG, WebP, and GIF images initially. Fetch images using server credentials and serve cached copies through Navigator asset routes.

### Homepage and replay

Extend the existing homepage into responsive cards containing preview image, logo, organization name, domain, description, and capture coverage derived from accepted indexes. Label coverage “Captures from … to …” without implying continuous preservation.

The main card link opens:

- The nearest available capture of `featured_capture.url` when provided, preferring the earlier capture on equal-distance ties.
- Otherwise, the latest available capture of `homepage`.
- The archive search page with an explanation when the selected URL has no captures.

Include a separate “Browse captures” link. Select entry captures from the accepted archive index, independent of Wayback fallback. Preserve the existing fallback option and default.

Provide image placeholders, escaped metadata text, responsive sizing, and visible keyboard focus.

### Refresh and failure isolation

Retain the supervised pywb process. Add a Navigator-owned frontend adapter for catalog presentation, cached assets, and activation of recovered archive routes. Publish presentation state atomically for the frontend.

Refresh manifests, images, and indexes using the existing polling interval and object-change detection. Server catalog edits require a restart. Display metadata refreshes automatically; changing an accepted archive ID requires a restart.

Malformed server configuration and duplicate archive IDs detected at startup fail clearly. Bucket-specific failures leave unavailable cards while healthy archives continue serving. Use the bucket/prefix as the label when metadata is unavailable.

Retry failed entries and activate them after recovery. Reject ID conflicts discovered during recovery without disrupting existing archives. Preserve last-valid cached metadata and indexes after refresh failures; missing images alone must not disable playback. Cached indexes cannot guarantee replay when remote WARCs are inaccessible.

## Migration and boundaries

- Replace existing Navigator configuration examples and documentation with catalog JSON and bucket manifests.
- Remove `navigator.toml` examples and directory-catalog instructions.
- Document moving existing flat bucket data into `data/`: copy, verify, switch configuration, then explicitly remove old objects. Do not automatically move or delete existing bucket contents.
- Detect legacy flat archive data before Fetch synchronization/reset or Navigator loading and report the required migration instead of silently treating it as an empty archive.
- Keep local archive authoring and Fetch logging behavior intact.
- Bucket-backed catalogs are the supported Navigator interface for this release; retire the previous local-source CLI path.
- Defer Fetch disk offloading/resumption, automatic thumbnails, metadata editors, multiple storage credential environments, and production hosting hardening.

## Verification and acceptance

- Test JSON contracts, optional metadata, both image objects, prefix resolution, traversal rejection, duplicate IDs, and obsolete-interface diagnostics.
- Verify data uploads and resets preserve manifests, assets, logs, and unrelated objects.
- Test legacy flat-layout detection and the documented migration procedure.
- Verify cache isolation, metadata/image refresh, atomic updates, and retention after invalid or interrupted refreshes.
- Exercise unavailable buckets, missing images, recovery without restart, and an all-unavailable catalog.
- Test capture coverage, featured-capture selection, latest-homepage selection, and search fallback.
- Extend the authenticated S3 integration fixture for multiple buckets, metadata, images, listings, and WARC range responses.
- Start Navigator with an empty cache and no Fetch files; verify two websites appear and replay from their respective buckets.
- Run Navigator unit and real-pywb integration suites plus affected Fetch tests. Visually inspect desktop/mobile layouts and keyboard navigation.
