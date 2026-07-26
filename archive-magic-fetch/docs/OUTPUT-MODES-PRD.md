# PRD: Fetch output modes (WARC + loose files)

**Status:** Draft for implementation handoff  
**Scope:** `archive-magic-fetch` only  
**Date:** 2026-07-24  
**PR expectation:** One PR  

> **Extended by** [`WEBSITE-FILES-BROWSING-PRD.md`](./WEBSITE-FILES-BROWSING-PRD.md)
> (newest-wins path collisions, query-string folding for `website/`, optional
> `--rewrite-local`). That document is authoritative for browsable loose-file
> behavior; this PRD remains the source for the `--warc` / `--files` axes.

## 1. Summary

Extend `archive-magic-fetch` with two independent output axes:

1. **WARC + replay CDXJ** — `none` | `latest` | `all` (default **`all`**)
2. **Loose website files** — `none` (default) | `latest` | `unique` | `all`

Default behavior remains: discover IA captures in the date window, write full
WARC history + `replay/index.cdxj`. Loose files are opt-in.

**Deferred (out of scope for this PR):** include/exclude filters, list-only
mode, skip-existing / resume.

## 2. Goals

- Keep today’s archival product as the default (`--warc all`, `--files none`).
- Allow operators to materialize an inspectable website tree under `website/`.
- Allow a “single best capture per URL” mode for either or both outputs.
- Keep axes independent so combinations like `--warc all --files latest` work.
- Preserve existing discovery, provenance, retrieval, WARC semantics, and
  serial client lifecycle unless this PR explicitly changes them.

## 3. Non-goals

- Resume, append, overwrite, or merge into existing final outputs.
- Include/exclude URL filters.
- List-only / dry-run JSON mode.
- WACZ packaging.
- Saving HTTP headers/status sidecars next to loose files (bodies only).
- Changing collection naming, date defaults, or output root.

## 4. CLI contract

```text
archive-magic-fetch URL_PATTERN
  [--start DATE] [--end DATE]
  [--warc {none,latest,all}]
  [--files {none,latest,unique,all}]
```

| Flag | Values | Default |
| --- | --- | --- |
| `--warc` | `none`, `latest`, `all` | `all` |
| `--files` | `none`, `latest`, `unique`, `all` | `none` |

Existing flags unchanged:

- `--start` default `1995`
- `--end` default current UTC CDX timestamp
- Output root remains repository-sibling `archives/`

### 4.1 Validation

- If `--warc none` and `--files none`: print a clear message that nothing is
  selected, exit successfully (no discovery required preferred; if discovery
  already ran in a given implementation order, still do not write outputs).
  Recommended message: `Nothing to do: both --warc and --files are none`.
- Axes are independent; any other combination is valid, including the intended
  future operator default of `--warc all --files latest`.

## 5. Capture selection

Selection happens **after** full discovery and provenance publication, and
after urlkey grouping.

Date bounds remain inclusive CDX `from`/`to` semantics as today.

### 5.1 Mode `all`

For each urlkey group, keep **all** captures in the window (current behavior
for WARC export).

### 5.2 Mode `latest`

For each urlkey group, keep **exactly one** capture:

1. Prefer the newest capture whose CDX status is `200`.
2. Else prefer the newest capture whose status is present and not `3xx`.
3. Else omit the group (do not select a redirect-only URL).

**Redirect clarification:** “Include redirects” means whether a `301`/`302`
**record** may be chosen as the capture for that URL — not whether Fetch
follows the redirect to store the destination page. Destination URLs are
separate urlkeys and are selected independently if present in CDX.

For `--warc latest`, write **one WARC response record per selected URL**.

Playback failures for a selected capture still warn/skip as today.

## 6. Output layout

Collection root remains `archives/<collection>/`.

### 6.1 Provenance (`sources/`)

Unchanged: always publish a new `sources/wayback/<acquisition>/` after
successful non-empty discovery when the command will produce at least one
output mode. If both modes are `none`, skip discovery/provenance.

Discovery still stores the **full** CDX result for the query window, not the
post-`latest` subset. Selection is an export transform.

### 6.2 WARC + replay (`archive/`, `replay/`)

When `--warc` is `all` or `latest`:

- Write WARCs under `archive/` using existing readable-path / collision-bucket
  rules.
- After successful WARC export, generate and publish `replay/index.cdxj` as
  today.
- Preflight remains fatal on existing final WARC/replay targets (no resume in
  this PR).

When `--warc none`:

- Do not create/open WARCs.
- Do not create `replay/index.cdxj`.
- Skip WARC preflight / replay indexing stages.

### 6.3 Loose files (`website/`)

When `--files` is enabled, write bodies under:

```text
archives/example.com/website/
```

Not under `archive/`. Paths mirror the original site path (Ruby-style), with
directory-like URLs materialized as `index.html`.

#### Latest (`--files latest`)

No timestamps in paths. Host is the first segment under `website/` so
multi-host collections remain distinct:

```text
website/example.com/index.html
website/example.com/about/index.html
website/example.com/css/style.css
website/example.com/images/logo.png
```

#### All (`--files all`)

Host first, then capture timestamp, then the logical site path (Ruby
`--all-timestamps` spirit, adapted for multi-host collections):

```text
website/example.com/20060715085250/index.html
website/example.com/20060715085250/css/style.css
website/example.com/20051120005053/index.html
```

Identical planned paths with distinct digests append `--<digest8>` before the
filename extension.

#### Unique (`--files unique`)

Use the timestamped layout from `all`, but write only the first full response
for each valid digest within a URL group. Captures represented as WARC revisits
do not receive loose files. Missing or malformed digests remain independent.

#### Path rules for loose files

- Bodies only (decoded semantic payload from `get_memento` / same bytes that
  would feed a WARC response body).
- No header/status sidecar files in this PR.
- Directory URLs and extension-less directory-like paths → `.../index.html`
  (same heuristic spirit as the Ruby tool).
- Apply the same safety encoding / filesystem limit mindset used for readable
  archive paths so path components cannot escape `website/`.
- File-vs-directory conflicts: reshape like the Ruby tool when a file path
  must become a directory (`existing-file` → `existing-file/index.html`), or
  fail with a clear error if reshaping would overwrite an existing final file.
  Prefer deterministic reshaping when safe; do not silently clobber.
- Existing final loose-file paths remain fatal to overwrite in this PR (same
  no-resume policy as WARCs).
- Empty / failed playback bodies: do not leave empty files; count as playback
  failures consistent with WARC export warnings.

## 7. Pipeline

Updated successful flow:

```text
parse args + defaults
  -> if warc=none and files=none: message + exit 0
  -> derive collection layout
  -> WaybackSession / WaybackClient
  -> discover (full window)
  -> if empty: "No captures found" + exit 0
  -> publish sources/ acquisition (full discovery)
  -> group by urlkey
  -> build warc_selection from --warc (all|latest|none)
  -> build files_selection from --files (all|unique|latest|none)
  -> if warc enabled: preflight WARC/replay targets
  -> if files enabled: preflight website targets for planned paths
  -> export WARC and loose-file selections in one URL-group worker pass
  -> if warc enabled: build replay CDXJ
  -> print aggregate summary
```

WARC and loose-file selections are independent, but their writes share one
retrieval. `unique` exposes the same response/revisit classification used by
WARC export; `all` also materializes revisit bodies at every timestamp.

## 8. Summary / console

Extend the aggregate summary to report both outputs, for example:

```text
Summary: 235 selected for warc (all); 226 responses; 9 redirects omitted; ...
Files: 180 written (latest); 2 playback failures
```

Exact wording may vary; must make modes and counts obvious.

Keep compact progress lines. Do not add verbosity flags in this PR.

## 9. Implementation guidance

Prefer extending the existing flat package rather than a new adapter hierarchy:

| Area | Likely touch |
| --- | --- |
| `cli.py` | New flags, both-none short-circuit, stage gating |
| `discovery.py` or new small helper | `latest` selection per urlkey |
| `export.py` / `retrieval.py` | WARC response writing; warc gated by mode |
| `paths.py` | `website/` layout helpers, file path planning, preflight |
| new module optional | `files.py` for loose-file writing |
| tests | Mode matrix, latest preference, path layouts, both-none |

Do not reintroduce `cdx_toolkit` or raw playback. Keep pinned `wayback`,
`warcio`, and `cdxj-indexer` usage.

## 10. Acceptance criteria

1. Default CLI with no new flags still performs `--warc all --files none` and
   matches current WARC+CDXJ behavior for the same inputs.
2. `--warc none --files none` exits 0 with a clear no-op message and writes
   nothing.
3. `--warc latest` writes one response per selected URL (prefer 200) and builds
   `replay/index.cdxj`.
4. `--warc none` writes no `archive/` WARCs and no `replay/index.cdxj`.
5. `--files latest` writes bodies under `website/` without timestamp segments;
   directories become `index.html`.
6. `--files all` writes `website/<timestamp>/...` paths.
7. `--warc all --files latest` produces both artifact trees from one discovery
   run.
8. Provenance under `sources/` still reflects the full discovery set.
9. Existing final outputs still cause fatal preflight errors (no resume).
10. Offline deterministic tests cover selection + path layout + mode gating.
11. `uv run --package archive-magic-fetch pytest` passes.
12. Architecture doc updated to describe the new flags and `website/` area.

## 11. Open points resolved for this draft

| Topic | Decision |
| --- | --- |
| WARC default | `all` |
| Files default | `none` |
| Axes | Independent |
| Both none | Successful no-op with message |
| Latest status policy | Prefer newest 200; else newest non-3xx; else omit |
| WARC latest records | One response per URL |
| Loose file contents | Bodies only |
| Loose file root | `archives/<collection>/website/` |
| All-files timestamps | Prefix directories under `website/` (Ruby-compatible) |
| Resume / filters / list | Deferred |

## 12. Suggested test matrix (minimum)

- CLI defaults parse to warc=all, files=none
- both none → exit 0, no network client required (or client unused)
- latest selection: 200 beats newer 404; 200 beats newer 301; only-301 omitted
- files latest paths: `/` → `website/example.com/index.html`; `/a/b/` → `website/example.com/a/b/index.html`
- files all paths: include host + 14-digit timestamp directories
- multi-host collections keep distinct `website/<host>/...` trees
- warc none + files latest: website written, no replay index
- warc latest + files none: single responses, replay index present
- dual mode shared capture fetched once (fake client call count)
