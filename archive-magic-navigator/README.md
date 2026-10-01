# Archive Magic Navigator

Navigator serves Archive Magic WARC collections through a local pywb viewer. It is
independent from Archive Magic Fetch and reads its own per-archive `navigator.toml`.

## Install

```console
uv sync
```

## Serve one archive

Pass a configuration file or its containing directory:

```console
uv run archive-magic-navigator ~/archives/example.org --open
```

Each `navigator.toml` selects exactly one source. A local source reads WARCs from its exact `directory`. Both source types cache
validated CDXJ snapshots. A remote source serves WARC ranges from the bucket.

Useful process options are:

```text
--cache PATH
--poll-interval SECONDS
--bind ADDRESS
--port PORT
--wayback-fallback {on,off}
--open
--debug
```

Navigator checks both local and remote sources every five minutes by default.
Use `--poll-interval 60` for a one-minute interval. Updated annual indexes and new
annual collections appear in the running viewer after a successful refresh.

The default index cache is `navigator-cache/` beside `navigator.toml` (or under the
catalog directory). Local mode also needs a writable cache; `--cache PATH` overrides
its location. Only indexes are cached, never WARC payloads. Navigator validates all
changes before atomically switching the archive's merged replay index. Listing,
copying, downloading, validation, or publication failures keep the previous replay
snapshot available and retry at the next interval. Missing annual indexes are
retained for the running session.

Annual source files and configuration formats are unchanged. Publish WARCs before
atomically replacing CDXJ files. Existing WARC byte ranges must remain available;
index snapshots cannot protect against deletion or destructive rewriting of WARCs.
Adding catalog entries or changing `navigator.toml` still requires a restart.

## Serve a catalog

```console
uv run archive-magic-navigator --catalog ~/archives
```

Catalog discovery includes only immediate, non-hidden `*/navigator.toml` entries.
Entries are sorted deterministically, and any invalid configuration or duplicate ID
fails startup. Mixed local/remote entries are supported. Remote entries must share
endpoint and region because pywb receives one S3 environment; their buckets and
prefixes may differ.

## Playback policy

`--wayback-fallback` defaults to `on` for the whole process. Pass `off` to disable
it for every selected archive in that invocation.

## Credentials and exposure

Private bucket access uses Boto3/pywb's standard AWS credential chain. Navigator
does not load an adjacent `.env` file.

Navigator defaults to `127.0.0.1`. A non-loopback bind exposes an unauthenticated
development archive server and prints a warning; the application does not provide
TLS or hostile-content hardening.

See [the repository README](../README.md), the
[example configuration](../examples/example.org/navigator.toml), and the
[architecture document](docs/ARCHITECTURE-NAVIGATOR.md) for complete details.

## Tests

```console
uv run pytest -q -m 'not integration'
```

Run `uv run pytest -q -m integration` where local loopback socket binding is
permitted.
