# PRD: Browsable loose-file output (collisions, queries, local rewrite)

**Status:** Draft for implementation handoff  
**Scope:** `archive-magic-fetch` only (`--files` path planning, writing, optional
rewrite; docs)  
**Date:** 2026-07-24  
**PR expectation:** One PR  
**Depends on:** Output modes (`--warc` / `--files`) already implemented  

## 1. Summary

Improve `--files` output so a downloaded site tree is closer to a usable local
mirror:

1. **Newest-wins** when multiple selected captures map to the same website path
   (e.g. `/` vs `/index.html`).
2. **Query-string folding** for loose-file paths so assets like
   `main_style.css?v=1` land on `main_style.css` (and collide via newest-wins)
   instead of `main_style.css%3Fv%3D1`.
3. **Optional `--rewrite-local`** to rewrite HTML/CSS/JS references into local
   relative links after files are written, plus architecture documentation.

Default CLI behavior without the new flag remains: no rewrite. WARC / replay /
provenance behavior is unchanged except where noted for files-path planning.

## 2. Motivation (observed on swensethlawoffice.com)

With `--files latest`:

- `/` and `/index.html` both mapped to `index.html`, producing digest-suffixed
  duplicates (`index--55TF45FL.html`, `index--ATE3T3AL.html`) instead of one
  homepage.
- HTML used root-relative URLs (`/files/...`, `/contact.html`) that break under
  `file://`.
- Cache-busted assets were stored as percent-encoded queries
  (`main_style.css%3F1546028705`), which do not match HTML `href`/`src` query
  forms even under a local HTTP server.

## 3. Goals

- One file per website path for `--files latest` after collision resolution.
- Prefer the newest capture timestamp when paths collide.
- Fold query strings out of loose-file filenames so common static assets share
  one path and newest-wins applies.
- Optional post-write rewrite so operators can browse without a server when they
  opt in.
- Document the browsable-files contract in `ARCHITECTURE-FETCH.md`.

## 4. Non-goals

- Changing WARC urlkey grouping, WARC paths, or replay CDXJ.
- Resume / skip-existing / include-exclude / list-only (still deferred).
- Perfect offline fidelity for every JS-driven site.
- Rewriting absolute third-party URLs (CDNs, analytics) to local files.
- Downloading missing link targets discovered only during rewrite.
- Windows-specific alternate query encoding beyond existing safety encoding.
- Making `--rewrite-local` the default.

## 5. CLI contract

```text
archive-magic-fetch URL_PATTERN
  [--start DATE] [--end DATE]
  [--warc {none,latest,all}]
  [--files {none,latest,all}]
  [--rewrite-local]
```

| Flag | Meaning | Default |
| --- | --- | --- |
| `--rewrite-local` | After successful `--files` writing, rewrite HTML/CSS/JS under `website/` for local relative browsing | off |

Validation:

- `--rewrite-local` with `--files none` → exit 2 (usage error) with a clear
  message: rewrite requires `--files latest` or `--files all`.
- `--rewrite-local` alone does not enable files mode.

## 6. Newest-wins path collisions

### 6.1 When

After preferred website paths are computed (including query folding below), and
before digest-suffix disambiguation:

If two or more **selected** captures for the current `--files` mode map to the
same filesystem-equivalent website path, keep **exactly one**: the capture with
the newest aware UTC `timestamp`. Drop the others from the files plan (do not
fetch/write them for files mode).

Ties on identical timestamps: keep the lexicographically smaller
`(urlkey, original, digest)` tuple for determinism.

### 6.2 Applies to

- `--files latest` (primary case: `/` vs `/index.html`).
- `--files all` when collisions remain after the timestamp directory segment
  (rare; still newest-wins among same final path).

### 6.3 Digest suffixes

- **Do not** digest-suffix collisions that newest-wins can resolve.
- Retain digest-suffix disambiguation only when newest-wins still leaves multiple
  distinct captures at the same path **and** identical timestamps (true
  same-path/same-time/different-digest). If that remains after the tie-break
  above, suffix as today.

### 6.4 Example

```text
/           20260511051943  -> website/example.com/index.html  (kept)
/index.html 20260420004433  -> website/example.com/index.html  (dropped)
```

Result: single `website/example.com/index.html`.

## 7. Query-string folding

### 7.1 Path mapping change

For loose-file path planning only (not WARC):

1. Parse the capture `original` URL.
2. Build site path segments from **path only**.
3. **Ignore the query string** when forming the filename/directory segments.
4. Fragments remain ignored (already true via `urlsplit`).

Examples:

```text
/files/main_style.css?1546028705  -> website/<host>/files/main_style.css
/files/main_style.css?1719345030  -> website/<host>/files/main_style.css
/page?id=1                        -> website/<host>/page/index.html
                                   (extension-less / directory-like rules unchanged)
/search/?q=law                    -> website/<host>/search/index.html
```

Different queries for the same path therefore collide and are resolved by
**newest-wins**.

### 7.2 Encoding

- Do not embed `?` or percent-encoded query text in website filenames.
- Continue to safety-encode path components as today (`?` should not appear).
- Match Ruby’s browsable intent more closely than encoding cache-buster queries
  into the basename.

### 7.3 Scope note

Query folding is an intentional lossy transform for loose files. Distinct
query-specific HTML responses that share a path will not all be retained under
`--files latest`—only the newest. WARC mode continues to store each urlkey /
capture independently.

## 8. Optional `--rewrite-local`

### 8.1 When it runs

Only if `--files` wrote (or planned) a website tree and export reached the
rewrite stage without a fatal error. Run **after** all loose files for the
command are written.

If zero files were written (all skipped/failed), skip rewrite quietly.

### 8.2 Targets

Rewrite text files under `layout.website_root` whose extension is one of:

```text
.html .htm .css .js
```

(case-insensitive). Skip binary files; skip files that fail UTF-8/decode with a
warning and continue.

### 8.3 Rewrite rules (minimum viable)

For each file, rewrite references that point at **same-collection hosts**
represented in the website tree (host segments present under `website/`,
including `www.` variants normalized the same way as path hosting):

| Source form | Action |
| --- | --- |
| Root-relative `/path` | → relative path from the current file to `website/<host>/path` (folded query: strip query when resolving local targets) |
| Scheme-relative `//host/path` | treat as `https://host/path` then same-host rules |
| Absolute `http(s)://host/path` | if host normalizes to a website host segment, rewrite to relative; else leave unchanged |
| `url(...)` in CSS | same rules |
| HTML attributes `href`, `src`, `action` (and `srcset` URLs when straightforward) | same rules |

Do **not** rewrite:

- `mailto:`, `tel:`, `javascript:`, `data:`
- off-site hosts
- missing local targets: leave the original reference unchanged (do not invent
  files)

Directory links should prefer an existing `index.html` under that directory when
resolving.

Homepage: `/` resolves to `index.html` under the host root when present.

### 8.4 Implementation guidance

- Prefer a small dedicated module (e.g. `rewrite_local.py`) over growing
  `files.py` further.
- Deterministic relative paths using `posixpath.relpath` / `PurePosixPath`.
- In-place rewrite via temp file + exclusive replace consistent with publication
  helpers when practical; if a simpler write is used, do not leave truncated
  finals on failure.
- Idempotent enough that running rewrite twice does not corrupt already-relative
  links (skip URLs that are already relative without a scheme).

### 8.5 Console

Compact progress is enough, e.g.:

```text
Rewriting local links under website/...
Rewrote 120 files; 3 skipped (decode errors)
```

## 9. Pipeline changes

```text
... existing discovery / provenance / mode selection ...
  -> if files enabled:
       plan website paths with query folding
       resolve path collisions with newest-wins (+ rare digest suffix)
       preflight remaining targets
       write website files (shared retrieval cache as today)
  -> if --rewrite-local:
       rewrite under website/
  -> print summaries
```

WARC stages unchanged.

## 10. Documentation

Update `docs/ARCHITECTURE-FETCH.md` to describe:

- newest-wins collision policy for `website/`
- query folding for loose-file paths (WARC unaffected)
- `--rewrite-local` behavior, limitations (`file://` vs HTTP server, off-site
  URLs left alone, missing targets unchanged)
- example layouts showing single `index.html` and `main_style.css` without
  `%3F…`

Update `OUTPUT-MODES-PRD.md` only with a short “superseded / extended by
WEBSITE-FILES-BROWSING-PRD” pointer if helpful; do not duplicate the full spec.

## 11. Acceptance criteria

1. `/` and `/index.html` with different timestamps produce one
   `website/<host>/index.html` from the newer capture; no digest suffix.
2. Multiple `main_style.css?<token>` captures produce one
   `website/<host>/files/main_style.css` (newest body); no `%3F` filenames for
   those queries.
3. `--files latest` without `--rewrite-local` does not rewrite file contents.
4. `--rewrite-local` without `--files` fails with a clear usage error.
5. With `--files latest --rewrite-local`, root-relative `/contact.html` in a page
   under `website/<host>/` becomes a working relative link to `contact.html`
   when that file exists.
6. Off-site absolute URLs remain unchanged.
7. References to missing local paths remain unchanged.
8. WARC export paths and urlkey grouping unchanged by query folding.
9. Offline tests cover newest-wins, query folding, rewrite happy path, and flag
   validation.
10. `ARCHITECTURE-FETCH.md` documents the three behaviors.
11. `uv run --package archive-magic-fetch pytest` passes.

## 12. Suggested tests

- Path planning: `/` vs `/index.html` → one target; newer timestamp kept
- Path planning: `css?a` vs `css?b` → one `css` path; newer kept
- Path planning: identical timestamp + digest collision still suffixes (rare)
- CLI: `--rewrite-local --files none` → exit 2
- Rewrite: `href="/about.html"` from `website/h/dir/page.html` → correct
  relative target when `about.html` exists
- Rewrite: `https://cdn.example.com/x.js` unchanged
- Rewrite: missing `/nope.html` unchanged
- Integration-style temp tree: write + rewrite produces openable relative link
  structure without network

## 13. Resolved decisions

| Topic | Decision |
| --- | --- |
| Path collisions | Newest-wins by capture timestamp |
| Digest suffixes | Only for true same-path/same-time leftovers |
| Query strings in files paths | Fold away (ignore query) |
| Local rewrite | Optional `--rewrite-local`, off by default |
| Stock Ruby parity | Path/query folding closer to browsable intent; rewrite matches popular forks’ `--local`, not stock hartator 2.3.1 |

## 14. Out of scope follow-ups

- Skipping 404/junk CDX rows in files mode by default
- `--local-only` rewrite of an existing tree without fetching
- `srcset` / inline style / complex JS bundler edge cases beyond best effort
- Automatic `python -m http.server` helper command
