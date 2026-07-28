# MIME Route Rewrite Review Handoff

**Status:** Decision implemented: remove MIME-derived local-link rewriting.

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

## Decision

The planned URL-to-file route map was removed. The optional `--rewrite-local`
pass deliberately leaves MIME-derived routes unchanged:

```text
href="/download/report/" -> href="/download/report/"
```

Conservative rewrites still resolve conventional HTML directory indexes and
explicit filenames when the local target exists. MIME-aware filename planning,
response `Content-Type` validation, WARC output, and pywb replay are unchanged.

This keeps the optional convenience feature filesystem-driven and avoids
guessing MIME-derived suffixes or choosing among ambiguous planned routes.

## Verification baseline

The full `archive-magic-fetch` suite must continue to pass. Preserve MIME path,
response validation, WARC, replay CDXJ, redirect, and provenance tests.
