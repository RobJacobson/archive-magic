# Bucket catalog publication and migration

## New archives and presentation metadata

Keep `fetch.toml` at the local website root. Set `output.prefix` to the archive
root, not its `data/` child. Normal Fetch publication places WARC/CDXJ files in
that child. Keep logs local.

Prepare `archive.json` with required `id`, `name`, and `homepage`. The ID must
match archive filenames. Optional fields are `description`, `logo`, `preview`,
and `featured_capture`. Image objects have `src` and `alt`; their source keys are
relative to the archive root. Use PNG/JPEG/WebP/GIF, at most 8 MiB per image.
The manifest must be at most 1 MiB. Unknown fields are rejected.

Capture previews at a viewport width that suits the archived site's layout,
with modest side margins (for example, 800 pixels for a 750-pixel fixed-width
site). Keep the full page height; the source image need not have a fixed aspect
ratio. Navigator displays previews in a responsive 16:10 frame using
`object-fit: cover` and `object-position: center top`, cropping excess length
from the bottom while keeping the header visible.

Prefer the first visually complete capture on the chosen date. Exclude replay
toolbars from the screenshot. Choose the width separately for each website;
avoid a universal wide viewport that adds large empty margins to older sites.
Retain the full-height source image so presentation cropping can change in CSS
without recapturing or permanently cropping the asset.

A featured capture has an HTTP(S) `url` and 14-digit UTC `timestamp`:

```json
"featured_capture": {
  "url": "https://example.org/",
  "timestamp": "20120615000000"
}
```

Navigator chooses the nearest indexed capture of that URL, preferring an earlier
capture on a tie. Without this field, it selects the latest indexed homepage
capture. Capture date coverage comes from indexes rather than hand-entered metadata.

With a separately configured rclone remote named `r2`, publish explicitly:

```sh
rclone copy /path/to/example.org/assets r2:example-org/assets
rclone copyto /path/to/example.org/archive.json r2:example-org/archive.json
```

For a shared bucket, append the archive-root prefix to both destination paths.
Upload new assets before publishing the manifest that refers to them. Fetch's
data sync does not manage these files. Navigator is read-only; it does not edit
or upload metadata. Local authoring copies are not read by Navigator.

## Moving an existing flat bucket archive

Stop Fetch writers and plan a replay maintenance window. These instructions use
a dedicated `example-org` bucket; for a prefix, use its complete archive-root path.

1. List root-level objects and prepare a reviewed text file containing only the
   archive's WARC/CDXJ basenames, one per line. Exclude directories, assets, metadata,
   and other websites. Call it `/tmp/archive-files.txt` in the commands below.
2. Copy the selected files to `data/`; do not run sync or purge:

   ```sh
   rclone copy r2:example-org r2:example-org/data --files-from-raw /tmp/archive-files.txt
   ```

3. Verify the copies before deleting anything:

   ```sh
   rclone check r2:example-org r2:example-org/data --files-from-raw /tmp/archive-files.txt --one-way --download
   ```

   `--download` verifies content even when the provider lacks comparable hashes;
   this can transfer substantial data. Also compare the reviewed file list and
   byte sizes. Keep your existing local archive intact.
4. Prepare the new catalog and bucket manifest. Keep Fetch's prefix at the archive
   root; the new implementation appends `data/` automatically. Replace old Navigator
   TOML configurations with the JSON catalog.
5. Explicitly remove only the reviewed old root objects after successful verification:

   ```sh
   rclone delete r2:example-org --files-from-raw /tmp/archive-files.txt --dry-run
   rclone delete r2:example-org --files-from-raw /tmp/archive-files.txt
   ```

6. Start the new Navigator, check each website, and resume Fetch publication.

Both applications reject remaining root-level `.warc.gz` or `.cdxj` objects as a
legacy layout, including when a cache exists. No migration happens automatically.
Never delete the local Fetch data merely because remote copies exist: ordinary
Fetch sync still treats the local archive as authoritative.

## Server configuration migration

`navigator.toml`, positional archive arguments, and directory catalogs have been
removed. Use `archive-magic-navigator --catalog /path/to/catalog.json`; one entry
serves one website. The new server only serves bucket-backed catalogs. Metadata
is stored in each bucket, never duplicated in the server catalog. Catalog order
is display order. All entries share endpoint, region, and compatible read-only
credentials. Editing this server file requires a restart.
