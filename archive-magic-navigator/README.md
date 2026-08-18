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

Each `navigator.toml` selects exactly one source. A local source serves its exact
`directory`. A remote source caches validated indexes locally and serves WARC
ranges from the bucket.

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

The default remote cache is the visible `navigator-cache/` beside `navigator.toml`.
It retains the last validated index if polling observes an incomplete publication
or transient bucket failure.

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

Without a CLI override, every archive uses its own
`[playback].wayback_fallback` setting. A catalog may therefore have mixed policy.
`--wayback-fallback on` or `off` overrides all selected archives for that process.

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
