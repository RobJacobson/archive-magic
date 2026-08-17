# Private S3-compatible bucket smoke test

This procedure exercises a remote archive against a private bucket.
Use a disposable prefix: the reset step intentionally deletes it.

## Configure

1. Copy `examples/example.org/fetch.toml` and
   `examples/example.org/navigator.toml` outside either implementation directory.
2. In `fetch.toml`, set `output.type = "remote"` and add:

   ```toml
   bucket = "your-private-bucket"
   prefix = "archive-magic-smoke/example.org"
   endpoint_url = "https://your-s3-compatible-endpoint"
   region = "auto"
   ```

3. In `navigator.toml`, set `source.type = "remote"` with the same bucket fields
   (no `directory` on a remote source).
4. Limit `[fetch]` to a small historical interval for the smoke test. Keep
   `data_directory = "data"` so transient working files remain isolated from the
   configuration and logs.
5. Configure credentials through the standard AWS/Boto3 chain (for example an AWS
   profile or process environment). Archive Magic does not load `.env`.

## Publish and play

From the Fetch project:

```console
uv run archive-magic-fetch /absolute/path/to/example.org/fetch.toml
```

Confirm the prefix contains WARC/CDXJ objects and
`collections-manifest.json`. Confirm `data/` contains no finalized WARC or CDXJ
after success. Re-run Fetch without source changes and
confirm it downloads only the active CDXJ/tail and performs no WARC/index uploads.

From the Navigator project:

```console
uv run archive-magic-navigator /absolute/path/to/example.org --open
```

Confirm that `navigator-cache/` contains manifest/index files, no WARC copies, and
that replay produces authenticated WARC range reads from the bucket.

## Update and continuity

Extend the selected source interval or wait for a new snapshot, then run Fetch
again. The expected order is changed/new WARC, live CDXJ, manifest verification,
the run record, then local-working-file cleanup. Earlier WARC objects must be
untouched; an existing tail must be an exact prefix extension.

To exercise recovery, interrupt or fail one upload and confirm the finalized
WARC/CDXJ remain in `data/`. The next run must finish through the normal
materialize/index/publish path. Do not delete `data/` during an incomplete
publication; without it, reset and regeneration are required.

Keep Navigator running during the update. It should use the previous validated
index until the new manifest is committed, then adopt the new index on a later poll.

## Destructive reset

The explicit flag is authorization and does not prompt:

```console
uv run archive-magic-fetch /absolute/path/to/example.org --reset-data
```

Remote reset rejects `--start`/`--end`, prints a downtime warning, deletes only the
complete configured prefix, clears the managed data directory, and rebuilds the
full configured range. Confirm a neighboring prefix remains untouched.
