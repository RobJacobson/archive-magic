# Archive Magic

Archive Magic consists of two independent applications:

- `archive-magic-fetch` discovers captures, retrieves them, and builds WARC/CDXJ collections.
- `archive-magic-navigator` plays one or more collections through pywb.

They do not communicate with each other. Each application has its own configuration
file. The archive protocol is the flat directory of WARC and CDXJ files. Configuration
files may be colocated for convenience but are not archive content and need not exist
on the same host.

## Configuration

Copy [`examples/example.org/fetch.toml`](examples/example.org/fetch.toml) and
[`examples/example.org/navigator.toml`](examples/example.org/navigator.toml) to a
directory you control and edit them. Relative paths are resolved from the
containing TOML file. `data_directory` / `directory` is the exact data path, not a
parent to which the archive ID is appended.

For example, this layout keeps high-volume data visible and separate from either
implementation checkout:

```text
archives/
  example.org/
    fetch.toml
    navigator.toml
    data/
      example.org-2004-001.warc.gz
      example.org-2004-index.cdxj
    logs/
      <run-id>.json
      <run-id>.log
    navigator-cache/       # created only for remote playback
```

For both Fetch output modes, the finalized WARC/CDXJ files under `data/` are
the source of truth. Remote output mirrors those files to the configured bucket
with rclone; Fetch never downloads its working archive from the bucket.

An explicit TOML path may use any filename. Passing a directory resolves
`<directory>/fetch.toml` or `<directory>/navigator.toml`:

```console
archive-magic-fetch /data/archives/example.org
archive-magic-navigator /data/archives/example.org
```

`~` is expanded, so `~/archives/example.org` works as expected.

Process policy is CLI flags with code defaults, not a settings file:

```text
archive-magic-fetch ARCHIVE [--workers N] [--starts-per-second N] [--retries N]
archive-magic-fetch ARCHIVE --sync-only
archive-magic-navigator ARCHIVE [--bind ADDRESS] [--port PORT]
  [--poll-interval SECONDS] [--cache PATH] [--wayback-fallback {on,off}]
```

Fetch defaults are 4 workers, 16 starts/second, and 4 retries. Navigator defaults
are `127.0.0.1:8080`, a 60-second poll interval, and Wayback fallback on.

## Local archive

Use `output.type = "local"` in Fetch and `source.type = "local"` in Navigator.
Fetch updates the flat `data/` directory, and Navigator serves that data directly:

```console
archive-magic-fetch ~/archives/example.org
archive-magic-navigator ~/archives/example.org --open
```

Without `--start` or `--end`, Fetch checks the configuration's complete configured
history (`fetch.start` through `fetch.end`, or now). Completed older years remain
unchanged when the source has no newly discovered captures.

## Remote archive

Use `output.type = "remote"` in Fetch and `source.type = "remote"` in Navigator,
with bucket fields in each file. Credentials are read through Boto3's standard
credential chain in Navigator and by rclone from the AWS environment or profile
in Fetch. Archive Magic does not load an adjacent `.env` file.

```console
archive-magic-fetch ~/archives/example.org
archive-magic-navigator ~/archives/example.org --poll-interval 60
```

The local `data/` directory is the source of truth. Fetch builds each year in
`data/.staging/`, promotes validated WARC files and their CDXJ to `data/`,
then waits for rclone to mirror that year before starting the next. It copies
WARCs first, updates the CDXJ, and only then removes obsolete bucket WARCs.
Fetch writes one JSON record and one console log per invocation under `logs/`.
Navigator keeps indexes in the visible `navigator-cache/`, streams WARC ranges
from the bucket, and continues using its last validated index during an incomplete
publication or transient bucket error.

Install rclone on the Fetch host. The existing bucket, prefix, endpoint, and
region fields in `fetch.toml` configure it; AWS environment credentials or an
AWS profile supply authentication. After an upload failure, rerun
`archive-magic-fetch ARCHIVE --sync-only` to reconcile the complete local
archive without contacting Wayback. Sync refuses a missing or empty local
archive. One Fetch or sync process at a time owns the archive.

Before upgrading an existing remote archive, finish any pending old-style
publication, restore all bucket WARC/CDXJ files to the local `data/` directory
once with rclone, and compare the local and remote file sets. Future syncs treat
missing local managed files as deletions from the bucket.

## Dates, rollover, and reset

The default compressed WARC target is 250,000,000 bytes and can be changed per
Fetch configuration. An update may extend only the current collection's final WARC
as an exact byte prefix; rollover creates a new WARC and leaves earlier objects
alone.

CLI dates may only narrow the project range. A start before `fetch.start`, an end
after the configured or resolved project end, or a reversed range is rejected:

```console
archive-magic-fetch ~/archives/example.org --start 2026-01-01 --end 2026-12-31
```

`--reset-data` is exceptional maintenance. With remote output it rejects date
overrides, deletes the complete configured prefix, clears `data/`, and
rebuilds the full configured range. Playback is unavailable until the new indexes
are published. With local output it preserves the existing selected-collection
reset behavior.

## Catalog playback

A catalog contains immediate, non-hidden child directories with `navigator.toml`:

```text
archives/
  example.org/navigator.toml
  example.net/navigator.toml
```

Serve it with:

```console
archive-magic-navigator --catalog ~/archives
```

Entries are sorted by directory name. Invalid configurations and duplicate archive
IDs fail startup. Remote catalog entries must share endpoint and region, and the
Navigator process must be able to use one common credential environment; buckets
and prefixes may differ.

See the component architecture documents for publication recovery and playback
details.
