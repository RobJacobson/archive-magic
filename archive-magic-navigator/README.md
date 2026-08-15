# Archive Magic Navigator

Navigator serves Archive Magic WARC collections through a local pywb viewer. It is
independent from Archive Magic Fetch and reads the same per-archive `archive.toml`.

## Install

```console
uv sync
```

## Serve one archive

Pass a descriptor or its containing directory:

```console
uv run archive-magic-navigator ~/archives/example.org --open
```

`--source auto` is the default: local-authoritative descriptors serve their exact
`workspace_directory`, while remote-authoritative descriptors cache validated
indexes locally and serve WARC ranges from the bucket.

Useful process options are:

```text
--source {auto,local,remote}
--cache PATH
--poll-interval SECONDS
--bind ADDRESS
--port PORT
--wayback-fallback {on,off}
--open
--debug
```

The default remote cache is the visible `navigator-cache/` beside `archive.toml`.
It retains the last validated index if polling observes an incomplete publication
or transient bucket failure.

## Serve a catalog

```console
uv run archive-magic-navigator --catalog ~/archives
```

Catalog discovery includes only immediate, non-hidden `*/archive.toml` entries.
Entries are sorted deterministically, and any invalid descriptor or duplicate ID
fails startup. Mixed local/remote entries are supported. Remote-selected entries
must share endpoint and region because pywb receives one S3 environment; their
buckets and prefixes may differ.

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
[example descriptor](../examples/example.org/archive.toml), and the
[architecture document](docs/ARCHITECTURE-NAVIGATOR.md) for complete details.

## Tests

```console
uv run pytest -q -m 'not integration'
```

Run `uv run pytest -q -m integration` where local loopback socket binding is
permitted.
