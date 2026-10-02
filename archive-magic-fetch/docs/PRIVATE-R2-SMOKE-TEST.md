# Private S3-compatible bucket smoke test

This procedure exercises a remote archive against a private bucket.
Use a disposable prefix: the reset step intentionally deletes its managed archive data.

## Configure

1. Copy `examples/example.org/fetch.toml` and
   `examples/catalog.json` outside either implementation directory.
2. In `fetch.toml`, set `output.type = "remote"` and add:

   ```toml
   bucket = "your-private-bucket"
   prefix = "archive-magic-smoke/example.org"
   endpoint_url = "https://your-s3-compatible-endpoint"
   region = "auto"
   ```

3. In `catalog.json`, set the shared endpoint/region and an entry with the same
   bucket and archive-root prefix. Publish `archive.json` and optional assets
   separately as described in the migration/publication guide.
4. Limit `[fetch]` to a small historical interval for the smoke test. Keep
   `data_directory = "data"` as the permanent local archive.
5. Install rclone and configure credentials through AWS environment variables
   or an AWS profile. Fetch derives rclone's bucket settings from `fetch.toml`;
   it does not load `.env`.

## Publish and play

From the Fetch project:

```console
uv run archive-magic-fetch /absolute/path/to/example.org/fetch.toml
```

Confirm the prefix's `data/` contains WARC and CDXJ objects and `data/` retains identical
finalized files. Re-run Fetch without source changes and confirm it does not
download archive files from the bucket.

From the Navigator project:

```console
uv run archive-magic-navigator --catalog /absolute/path/to/catalog.json --open
```

Confirm that `navigator-cache/` contains indexes and presentation assets, no WARC copies, and
that replay produces authenticated WARC range reads from the bucket.

## Update and continuity

Extend the selected source interval or wait for a new snapshot, then run Fetch
again. The expected order is local annual promotion, bucket WARC copy, bucket
CDXJ sync, then bucket WARC pruning. Earlier WARC objects must be untouched;
an existing tail must be an exact prefix extension.

To exercise recovery, interrupt or fail one upload and confirm the finalized
WARC/CDXJ remain in `data/`. Then run
`archive-magic-fetch /absolute/path/to/example.org --sync-only` and confirm
it completes publication without contacting Wayback.

Keep Navigator running during the update. It should use the previous validated
index until the new CDXJ is committed, then adopt the new index on a later poll.

## Destructive reset

The explicit flag is authorization and does not prompt:

```console
uv run archive-magic-fetch /absolute/path/to/example.org --reset-data
```

Remote reset rejects `--start`/`--end`, prints a downtime warning, deletes only the
selected archive's managed files under remote `data/`, clears the local data directory, and rebuilds the
full configured range. Confirm archive.json, assets, unrelated files, and neighboring prefixes remain untouched.
