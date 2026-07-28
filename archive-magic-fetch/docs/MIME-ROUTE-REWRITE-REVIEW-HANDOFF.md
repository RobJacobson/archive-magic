# MIME Route Rewrite Review Handoff

**Status:** Review requested; do not implement changes without approval.

**Goal:** Suggest the smallest correct design for local link rewriting after
MIME-aware loose-file naming.

## Required behavior

Loose files must keep their correct types:

```text
/about                    text/html        -> about/index.html
/download/annual-report   application/pdf  -> download/annual-report.pdf
/download/report/         application/pdf  -> download/report.pdf
/                         application/pdf  -> index.pdf
```

CDX MIME plans the destination. The retrieved response `Content-Type` validates
it; a destination-changing mismatch skips only the loose-file write. WARC and
pywb replay behavior must remain unaffected.

## Question to review

The current implementation also builds a planned URL-to-file route map so the
optional `--rewrite-local` pass can rewrite:

```text
href="/download/report/" -> href="download/report.pdf"
```

This adds plumbing across:

- `paths.py`: `WebsitePlan.include_timestamps`, `normalized_site_path()`, and
  `website_route_map()`
- `cli.py`: route-map construction
- `rewrite_local.py`: route-map parameters through the rewrite helpers
- route-specific tests

Correct `.pdf` naming and MIME mismatch protection do **not** depend on this
route map. Only best-effort local link rewriting does.

## Requested analysis

Please compare these options and recommend one:

1. Keep the route map, but reduce its code and parameter plumbing.
2. Move route resolution entirely into `rewrite_local.py` with a smaller input.
3. Remove MIME-derived link rewriting and leave such links unchanged while
   retaining correct filenames.
4. Propose a smaller alternative that does not guess MIME or scan ambiguously
   for suffixes.

Evaluate:

- production LOC and conceptual complexity
- correctness for `latest`, `unique`, and `all` timestamp layouts
- query folding and multi-host collections
- behavior when a planned file was skipped after `Content-Type` validation
- whether any public helper or dataclass field can be removed

Favor KISS/YAGNI/DRY. The primary product is pywb replay; `--rewrite-local` is
an optional convenience. Provide suggestions and estimated deletions first,
without editing the repository.

## Verification baseline

The full `archive-magic-fetch` suite must continue to pass. Preserve MIME path,
response validation, WARC, replay CDXJ, redirect, and provenance tests.
