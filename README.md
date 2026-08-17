# Archive Magic

Archive Magic consists of two independent applications:

- `archive-magic-fetch` discovers captures, retrieves them, and builds WARC/CDXJ collections.
- `archive-magic-navigator` plays one or more collections through pywb.

They do not communicate with each other. Each application has its own configuration
file. The generated `collections-manifest.json` describes published artifacts; it
is state, not configuration. Configuration files may be colocated for convenience
but are not archive content and need not exist on the same host.

Both applications depend on the small, dependency-free `archive-magic-format`
package for this manifest protocol. It shares the archive format, not application
configuration or runtime behavior.

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
      collections-manifest.json
      example.org-2004-001.warc.gz
      example.org-2004-index.cdxj
    logs/
      <run-id>.json
      <run-id>.log
    navigator-cache/       # created only for remote playback
```

For remote Fetch output, finalized WARC/CDXJ working copies under `data/` are
removed after each successful publication; the bucket remains their source of
truth.

An explicit TOML path may use any filename. Passing a directory resolves
`<directory>/fetch.toml` or `<directory>/navigator.toml`:

```console
archive-magic-fetch /data/archives/example.org
archive-magic-navigator /data/archives/example.org
```

`~` is expanded, so `~/archives/example.org` works as expected.

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
credential chain; Archive Magic does not load an adjacent `.env` file.

```console
archive-magic-fetch ~/archives/example.org
archive-magic-navigator ~/archives/example.org --poll-interval 60
```

The bucket prefix is the source of truth. Before fetching, Fetch validates or
downloads the selected collection's CDXJ and final WARC into `data/`. It
runs the same append/index pipeline as local output, publishes changed
WARC/index objects, and commits `collections-manifest.json` last. After a verified
commit it removes the finalized local working copies. Fetch writes one JSON record
and one console log per invocation under `logs/`.
Navigator keeps indexes in the visible `navigator-cache/`, streams WARC ranges
from the bucket, and continues using its last validated index during an incomplete
publication or transient bucket error.

One Fetch process on one machine owns an archive prefix. Failed publications keep
their local WARC/CDXJ working files for the next run; losing that data during
an incomplete update requires reset and regeneration.

## Dates, rollover, and reset

The default compressed WARC target is 250,000,000 bytes and can be changed per
Fetch configuration. An update may extend only the current collection's final WARC
as an exact byte prefix; rollover creates a new WARC and leaves earlier objects
alone.

CLI dates temporarily narrow a normal run:

```console
archive-magic-fetch ~/archives/example.org --start 2026-01-01 --end 2026-12-31
```

`--reset-data` is exceptional maintenance. With remote output it rejects date
overrides, deletes the complete configured prefix, clears `data/`, and
rebuilds the full configured range. Playback is unavailable until the new manifest
is published. With local output it preserves the existing selected-collection
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
