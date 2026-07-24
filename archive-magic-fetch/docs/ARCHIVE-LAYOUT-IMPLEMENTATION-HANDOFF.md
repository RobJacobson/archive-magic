# Archive Collection Layout Implementation Handoff

**Status:** Approved design; implementation pending

**Date:** July 23, 2026

**Audience:** The agent that will prepare the implementation plan and implement
the change

**Scope:** `archive-magic-fetch`

## 1. Assignment

Implement the approved archive-collection layout, source-CDX provenance,
readable WARC allocation, collision buckets, and replay CDXJ generation
described in this note.

This is one coherent change. The final result should replace the current
hash-based `archives/urlkey/...` output with a self-contained collection for
the website selected by the command:

```text
archive-magic/
├── archive-magic-fetch/
└── archives/
    └── kevin.burke.dev/
        ├── sources/
        │   └── wayback/
        │       └── 20260723T184501.123456Z/
        │           ├── query.json
        │           └── captures.cdx.gz
        ├── archive/
        │   ├── index.warc.gz
        │   ├── about.warc.gz
        │   ├── posts/
        │   │   ├── index.warc.gz
        │   │   └── hello-world.warc.gz
        │   └── images/
        │       └── logo.png.warc.gz
        └── replay/
            └── index.cdxj
```

The final directory is deliberately named `replay/`, not `indexes/`.
`captures.cdx.gz` is also an index, so `indexes/` would leave the distinction
between upstream provenance and locally generated replay data ambiguous.

Keep all implementation under `archive-magic-fetch/`. The repository root
remains `archive-magic/`, and generated collections remain beneath its
`archives/` sibling directory.

After the implementation and deterministic tests pass, update
`docs/ARCHITECTURE-FETCH.md` to describe the implemented result. Do not treat
the architecture document's current hash-based layout, one-group-per-WARC
rule, persisted-inventory non-goal, or CDXJ non-goal as requirements; those
sections are what this change supersedes.

## 2. Product intent

The resulting collection should be understandable without knowledge of CDX
SURTs or Archive Magic's internal allocation rules:

- The top-level collection name looks like the website being saved.
- `sources/` records what the Internet Archive reported during discovery.
- `archive/` contains the WARC data created by Archive Magic.
- `replay/` contains indexes generated from those exact WARC bytes.
- WARC paths resemble URL paths and do not contain routine identity hashes.
- Rare filesystem-name collisions do not stop an unattended job and do not
  add noisy suffixes. Colliding resources share a WARC storage bucket and
  remain distinguishable by WARC metadata and the replay index.

The source CDX and replay CDXJ serve different purposes:

```text
Wayback CDX discovery
    -> sources/wayback/<acquisition>/captures.cdx.gz
       (upstream selection and provenance)

successful playback and WARC serialization
    -> archive/**/*.warc.gz
    -> replay/index.cdxj
       (local replay locations and byte ranges)
```

Do not use the IA source CDX as the replay index. It has no offsets or lengths
for Archive Magic's newly written WARCs.

## 3. Current code versus required code

| Concern | Current implementation | Required implementation |
| --- | --- | --- |
| Collection boundary | A literal `archives/urlkey/` namespace | One natural website directory directly beneath `archives/` |
| Host representation | Percent-encoded SURT authority such as `dev%2Cburke%2Ckevin%29` | Normal host order such as `kevin.burke.dev` |
| Scheme and `www` | Paths can be separated by the old URL representation | HTTP/HTTPS and Wayback-canonicalized leading `www.` variants share the collection |
| WARC path identity | Twelve hexadecimal SHA-256 characters are appended to every filename | No routine hash or numeric disambiguator |
| URL paths | URL-key segments are encoded under `urlkey/` | URL path segments are mirrored under the site's `archive/` directory |
| Collisions | `preflight_paths()` raises when two groups map to one path | Equivalent paths are grouped into one WARC storage bucket |
| Export ownership | `export_group()` opens and closes one WARC for one URL key | A bucket owns one WARC and may export several URL-key groups into it |
| Deduplication | Maps are local to one call of `export_group()` | Maps still reset between URL-key groups, including groups sharing a WARC |
| Source discovery | `discover()` returns an in-memory list only | A complete normalized discovery snapshot and manifest are persisted before playback |
| Replay index | None | Generate one sorted site-level `replay/index.cdxj` from closed WARC files |
| Architecture | Says persisted source inventories and replay indexes are non-goals | Describes both as implemented collection artifacts |

The recently implemented content policy is not part of this rewrite and must
remain intact:

- Omit known CDX 3xx rows before playback.
- Omit statusless rows that play back as 3xx.
- Warn and skip a known CDX/Memento status mismatch.
- Do not create unavailable-capture metadata or synthetic redirects.
- Preserve the aggregate selected/response/revisit/redirect/failure summary.

The source CDX snapshot must still contain redirects that are later omitted.

## 4. Collection naming

### 4.1 Normal form

Use the requested website scope to choose one collection directory for the
command. Normalize its host as follows:

1. Lowercase it.
2. Remove a trailing DNS dot.
3. Convert Unicode domains to their ASCII IDNA/punycode form.
4. Omit the scheme.
5. Remove a leading `www.` so the ordinary `www` and apex forms share a
   collection.
6. Retain a nondefault port as `--port-<number>`.

Examples:

```text
https://Kevin.Burke.Dev/     -> kevin.burke.dev
http://www.example.com/*     -> example.com
https://example.com:443/*    -> example.com
http://example.com:80/*      -> example.com
https://example.com:8443/*   -> example.com--port-8443
https://münich.example/*     -> xn--mnich-kva.example
```

The scheme's default port matters while parsing: `:80` is default only for
HTTP and `:443` only for HTTPS. A bare pattern without a scheme should continue
to be accepted if `wayback` accepts it. Do not introduce a public-suffix-list
dependency merely to derive a registrable domain.

For a supported domain pattern such as `*.example.com`, use the non-wildcard
scope (`example.com`) as the collection name. Its matching subdomains belong
to that acquisition. If a future form cannot yield one unambiguous website
scope, fail before writing rather than guessing.

The normalized CDX `urlkey` remains the resource-family identity. The readable
collection directory is organization, not a replacement identifier.

### 4.2 Safety

Apply the existing filesystem safety principles to the collection name:

- no path traversal or separators;
- no empty, dot, or dot-dot components;
- no control characters;
- no Windows reserved device names;
- no trailing dot ambiguity; and
- no Unicode normalization ambiguity.

Do not put a hash in a normal collection directory name. Two genuinely
different nondefault-port sites remain distinct through `--port-<number>`.

## 5. Persist the Wayback source acquisition

### 5.1 Timing and completeness

Write the source acquisition only after `WaybackClient.search()` has been
fully materialized successfully, including any complete-search retry already
owned by `discover()`. Write it before:

- value-equal duplicate collapse;
- redirect omission;
- URL-key grouping;
- playback;
- semantic deduplication; or
- WARC creation.

The snapshot is the normalized result exposed by the high-level
`WaybackClient.search()` API. Do not replace the public client with custom CDX
pagination or HTTP interception merely to save literal response bodies.

Consequently, preserve every `CdxRecord` in the returned discovery list,
including value-equal rows that Archive Magic later collapses and 3xx rows
that it later omits. Do not preserve resume keys, pagination-boundary transport
artifacts already handled by the client, HTTP headers, or failed partial
attempts.

An empty successful search should retain the current behavior (`No captures
found`) and need not create a site collection or source acquisition.

### 5.2 Acquisition identifier

Use a readable UTC acquisition time with microseconds, for example:

```text
20260723T184501.123456Z
```

Create the directory exclusively. In the extraordinarily unlikely event of an
identifier collision, allocate a deterministic numeric suffix such as `-2`,
`-3`, and so on. This suffix represents distinct acquisition runs; it is not a
resource-identity workaround and does not reintroduce noisy WARC names.

Use a temporary sibling directory and publish the complete acquisition
directory atomically where the platform permits. A failed source write must
not leave a final acquisition containing only one of its two required files.

### 5.3 `captures.cdx.gz`

Write UTF-8, gzip-compressed, line-oriented CDX with these seven fields:

```text
urlkey timestamp original mimetype statuscode digest length
```

Use the corresponding classic CDX field header:

```text
CDX N b a m s k S
```

Serialize timestamps in 14-digit UTC CDX form. Serialize an absent field as
`-`. Preserve discovery order. The file is provenance, so do not sort, collapse,
or rewrite rows according to final export outcomes.

The implementation should round-trip representative records in tests,
including absent status/digest values, non-ASCII URLs as returned by the
client, redirects, and value-equal duplicates. If a value cannot be represented
unambiguously as a CDX token, fail the source write rather than silently
producing a malformed snapshot.

### 5.4 `query.json`

Write a small, versioned manifest containing at least:

```json
{
  "schema_version": 1,
  "source": "internet-archive-wayback-machine",
  "url_pattern": "https://example.com/*",
  "date_start": "1995",
  "date_end": "20260723184501",
  "acquired_at": "2026-07-23T18:45:01.123456Z",
  "archive_magic_fetch_version": "0.1.0",
  "wayback_version": "0.5.1",
  "cdx": {
    "file": "captures.cdx.gz",
    "format": "CDX N b a m s k S",
    "fields": [
      "urlkey",
      "timestamp",
      "original",
      "mimetype",
      "statuscode",
      "digest",
      "length"
    ],
    "record_count": 235,
    "sha256": "<lowercase SHA-256 of the final captures.cdx.gz bytes>"
  }
}
```

Obtain installed package versions through package metadata rather than
duplicating version constants where practical. Use deterministic JSON
formatting so manifests are easy to diff. The acquisition timestamp and gzip
container metadata naturally prevent byte-for-byte reproducibility between
separate acquisitions; the checksum verifies the artifact actually saved in
this acquisition.

## 6. Readable WARC paths

### 6.1 Mapping

Map the path portion of each CDX URL-key resource family beneath the
collection's `archive/` directory:

```text
/                    -> archive/index.warc.gz
/about               -> archive/about.warc.gz
/posts               -> archive/posts.warc.gz
/posts/               -> archive/posts/index.warc.gz
/posts/hello-world    -> archive/posts/hello-world.warc.gz
/images/logo.png      -> archive/images/logo.png.warc.gz
```

These are URL path segments, not a claim about the origin server's filesystem.
`index` is the ordinary readable name for a root or trailing-slash resource;
do not use `_root` or `_index`.

Keep unsafe characters percent-encoded as single safe filesystem components.
Retain enough of a query-bearing URL key in the readable path to avoid
deliberately merging all query variants. For example, a query may be represented
by safe percent-encoding in the filename stem. It does not need a hash.
If two representations nevertheless normalize to the same path, the bucket
rule below handles it safely.

Remove `warc_path()` if it is no longer used. There should be one authoritative
mapping from the CDX resource-family identity to its preferred WARC bucket,
not parallel URL-based and URL-key-based naming systems.

### 6.2 Length limits

Bound encoded path components to a conservative cross-platform size. Truncate
deterministically without appending an identity hash. Any collision introduced
by truncation is safe because colliding groups share the resulting bucket.
Retain the `.warc.gz` suffix and as much readable leading text as practical.

Also guard against a path whose overall depth or encoded length cannot be
created on a supported filesystem. Resolve that condition deterministically
into a bounded bucket path or fail during preflight with a precise error; never
discover it hours later during playback. Do not use unchecked origin paths as
local paths.

### 6.3 Filesystem-equivalent paths

Treat paths conservatively when deciding whether two preferred names collide.
At minimum, account for:

- case-insensitive filesystems;
- the safe encoder's percent-escape normalization;
- trailing-dot behavior;
- reserved names; and
- component truncation.

Choose the displayed bucket spelling deterministically, independent of CDX
result order. Sorting candidate spellings and URL keys is sufficient; no
registry or persistent name-allocation database is needed.

## 7. WARC storage buckets

### 7.1 Allocation contract

Replace the current preflight result:

```python
dict[urlkey, Path]
```

with the conceptual inverse:

```python
dict[Path, list[urlkey]]
```

The exact type may be a small dataclass if it materially improves clarity, but
do not introduce a general storage-planning framework.

Preflight must:

1. compute the collection root;
2. compute every preferred readable WARC path;
3. group filesystem-equivalent paths into one bucket;
4. sort buckets by path and groups within a bucket by `urlkey`;
5. select one deterministic final spelling per bucket;
6. inspect every final WARC and replay target before Memento retrieval; and
7. fail on an existing final target under the current no-overwrite policy.

A generated name collision is a valid allocation, not an exception.

Examples of intentional shared buckets include:

```text
/posts/       -> archive/posts/index.warc.gz
/posts/index  -> archive/posts/index.warc.gz
```

and any collision caused by case folding, safe encoding, or deterministic
truncation.

Do not add `.1`, `.2`, `.3`, a digest, or another resource suffix. Multiple
resources in one WARC are valid and remain independently indexed.

An existing output file is different from an allocation collision. Continue
to reject existing WARCs, broken symlink targets, non-directory ancestors, and
an existing `replay/index.cdxj`. Do not overwrite, append, merge, or repair.

### 7.2 Export lifecycle

One bucket owns one lazily opened WARC:

1. Iterate its URL-key groups in sorted order.
2. For each group, retain the current timestamp ordering.
3. Initialize fresh source-signature and semantic-deduplication maps for that
   group.
4. Retrieve and write according to the existing export policy.
5. Reuse the bucket's already-open writer for later groups.
6. Close the WARC after all assigned groups finish.

The bucket gets one `warcinfo` record, not one per resource family.

Sharing storage must not introduce cross-URL-key deduplication. A response in
one group cannot seed a revisit shortcut in another group merely because the
groups share a file.

Keep WARC creation lazy. If every group assigned to a bucket contains only
omitted redirects or skipped captures, create no WARC for that bucket. The
replay index should describe only files and records that actually exist.

The current `export_group()` combines group policy with WARC ownership. Refactor
only as far as necessary:

- a bucket-level owner manages the stream/writer;
- a group-level helper creates and discards its own deduplication maps; and
- the existing summary accounting remains aggregate.

Avoid a new application/service layer.

## 8. Replay CDXJ

### 8.1 Required artifact

After all WARC streams have closed successfully, generate one sorted site-level
index:

```text
replay/index.cdxj
```

Generate it from the final WARC bytes, not from discovery records or in-memory
write counters. Index response and revisit records with their real compressed
offsets and lengths. The index must contain, where applicable:

- replay URL key;
- capture timestamp;
- target URL;
- MIME type;
- HTTP status;
- payload digest;
- WARC filename;
- compressed offset; and
- compressed record length.

It is acceptable for a revisit entry to omit an HTTP status if the WARC revisit
record has no embedded HTTP headers. Do not fabricate fields that are absent
from the WARC.

Sort by the normal CDXJ key and timestamp so pywb-style binary lookup works.
Multiple rows may name the same WARC file with different offsets.
If the export creates no WARC files, do not create an empty replay index.

### 8.2 Use the established indexer

Prefer the standalone
[`cdxj-indexer`](https://github.com/webrecorder/cdxj-indexer) rather than
implementing CDXJ canonicalization and compressed-record offset calculation
locally. It is designed for WARC/ARC indexing, extends `warcio`'s indexer, and
is the replacement recommended by
[pywb's indexing documentation](https://pywb.readthedocs.io/en/latest/manual/indexing.html).

Before pinning it, validate its current release against the project's supported
Python versions (3.12 minimum and the repository's selected 3.14 development
runtime) and the existing `wayback` dependency graph. Its current packaging
includes older transitive constraints, so record any incompatibility rather
than forcing an unsafe resolution. If compatible, add and pin it as a direct
runtime dependency and call its Python API through a small local wrapper.
Do not shell out to an executable from application code.

Configure its directory root so the CDXJ `filename` is relative to the site
collection and includes the `archive/` prefix:

```text
archive/index.warc.gz
archive/posts/index.warc.gz
archive/images/logo.png.warc.gz
```

Using only `Path.name` would be incorrect because many nested resources can
legitimately be named `index.warc.gz`.

Pass only the WARC files created by the current export. Do not recursively
index unrelated legacy files under `archives/urlkey/` or stale files from
another collection.

Write the CDXJ to a temporary file and publish it only after indexing succeeds.
Under the current nontransactional export policy, a fatal indexing error may
leave newly written WARCs but must not leave a truncated final
`replay/index.cdxj`.

If dependency compatibility makes `cdxj-indexer` unusable, stop and document
the concrete conflict before substituting a custom format implementation. The
fallback decision is architectural and should not be hidden inside the patch.

## 9. CLI orchestration

The new successful command flow should be:

```text
parse arguments and apply date defaults
    -> derive safe site collection name
    -> create shared Wayback session/client
    -> completely materialize discovery
    -> if empty: print "No captures found" and exit successfully
    -> save sources/wayback/<acquisition>/{captures.cdx.gz,query.json}
    -> collapse value-equal records and group by urlkey
    -> allocate and preflight WARC buckets plus replay/index.cdxj
    -> export buckets
    -> generate replay/index.cdxj from WARCs actually created
    -> print the existing aggregate content summary
    -> close the client/session
```

A successfully published source acquisition remains valid provenance if later
preflight, playback, WARC serialization, or replay indexing fails. Do not
delete it merely because the downstream export did not complete.

Do not add a general configuration system, output flag, archive-source
interface, concurrency, resume mechanism, or database as part of this work.

Consider whether the final summary needs one concise line naming the collection
and saved source/replay artifacts. Keep ordinary per-capture output no noisier
than it is today.

## 10. Suggested module changes

Keep the package flat. A reasonable division is:

| File | Change |
| --- | --- |
| `paths.py` | Replace hash/SURT output with collection normalization, readable resource paths, filesystem-equivalence keys, bucket allocation, and complete preflight |
| `discovery.py` | Keep search and grouping behavior; do not make it responsible for filesystem layout |
| `provenance.py` (new) | Serialize and atomically publish `captures.cdx.gz` and `query.json` |
| `export.py` | Export bucket-to-groups while resetting deduplication per URL key; report created WARC paths |
| `warc.py` | Continue owning low-level WARC creation and record serialization; no broad redesign required |
| `replay.py` (new) | Generate and atomically publish sorted `replay/index.cdxj` through the chosen indexer |
| `cli.py` | Orchestrate site naming, provenance, preflight, bucket export, and replay indexing |
| `pyproject.toml` / `uv.lock` | Add the validated pinned indexer dependency if compatible |

The new filenames are suggestions, not a demand for abstractions. If either
new module would contain only a trivial function, a clear existing module is
acceptable. All substantive Fetch code must remain under
`src/archive_magic_fetch/`.

## 11. Test requirements

Keep all tests deterministic and offline. Extend or replace the present path
and export tests to cover at least the following.

### 11.1 Collection and path tests

1. Natural host order replaces the encoded SURT directory.
2. Hostnames are lowercase, IDNA-normalized, and stripped of a trailing dot.
3. HTTP/HTTPS and leading `www.` normalize to one collection.
4. Default ports disappear; nondefault ports use `--port-<number>`.
5. Domain patterns yield one non-wildcard collection name.
6. Root, leaf, and trailing-slash URL keys produce the approved readable paths.
7. Query-bearing keys remain safely represented without hashes.
8. Empty, dot, dot-dot, separators, control characters, trailing dots, Windows
   reserved names, and encoded Unicode cannot escape or reshape the root.
9. Overlong components are bounded deterministically.
10. Filesystem-equivalent names map to the same bucket.
11. Preferred path spelling and group order do not depend on discovery order.
12. Existing WARC/replay targets, broken symlinks, and invalid ancestors fail
    before retrieval.

### 11.2 Provenance tests

13. A complete discovery list writes one gzip CDX with the exact seven-field
    header and one row per returned record.
14. Discovery order and value-equal duplicates are preserved.
15. Redirect rows and absent values are preserved.
16. The manifest contains the exact CLI bounds, pattern, versions, timestamp,
    field schema, row count, and a valid checksum of `captures.cdx.gz`.
17. A failed write publishes neither final source artifact.
18. An acquisition-ID collision is resolved safely without overwriting.
19. Empty discovery creates no acquisition.

### 11.3 Shared-WARC tests

20. Two noncolliding URL-key groups create two readable WARCs.
21. `/posts/` and `/posts/index` create one WARC with one `warcinfo` and both
    groups' records.
22. Deduplication works within each group but does not cross the group boundary
    inside a shared WARC.
23. Bucket and group output order is deterministic.
24. An all-skipped bucket creates no WARC.
25. Existing redirect, status-substitution, playback-failure, and summary
    behavior remains unchanged.

### 11.4 Replay-index tests

26. The generated CDXJ is sorted and has parseable JSON payloads.
27. Every response/revisit in every created WARC has the expected index entry.
28. `filename` is collection-relative and retains nested `archive/...` paths.
29. `offset` and `length` select the corresponding independently gzipped WARC
    record bytes.
30. Two records in one shared WARC have the same filename and distinct offsets.
31. Two nested `index.warc.gz` files remain distinguishable by their relative
    filenames.
32. A failed index build leaves no final `replay/index.cdxj`.
33. A full fixture collection can be parsed by `warcio`, and its replay index
    points only to files that exist.

Retain the existing discovery, retrieval, response/revisit, rate-limit, and
error-policy coverage unless an assertion concerns the deliberately replaced
layout.

## 12. Architecture-document revisions

Once code and tests pass, revise `docs/ARCHITECTURE-FETCH.md` comprehensively:

- Change the decision summary from one WARC per URL-key family to readable
  WARC buckets that usually contain one family and may contain colliding
  families.
- Replace the `archives/urlkey/...--<hash>.warc.gz` tree.
- Document `sources/`, `archive/`, and `replay/`.
- Describe collection-name normalization.
- Describe the source CDX snapshot and query manifest.
- Describe collision grouping and the per-group dedup reset.
- Describe final-WARC CDXJ generation and relative filenames.
- Update the CLI data-flow diagram.
- Update module responsibilities and the project tree.
- Replace path-collision fatality with bucket allocation.
- Remove persisted inventory and replay CDXJ from explicit non-goals and future
  enhancements.
- Add the indexer dependency and authoritative references if it is adopted.
- Preserve the current content-oriented redirect policy.

The architecture document should describe only the final implemented behavior,
not retain the old design as an alternative.

## 13. Non-goals

Do not expand this implementation into:

- migration or automatic rewriting of existing `archives/urlkey/` data;
- overwrite, merge, append, repair, or resume support;
- a generic archive-source interface;
- Common Crawl support;
- replay-server implementation or pywb configuration;
- source-WARC byte preservation;
- redirect preservation or unavailable-capture metadata;
- cross-URL-key payload deduplication;
- concurrent retrieval;
- sharded/ZipNum replay indexes;
- a database or persistent resource-name registry;
- a general manifest framework; or
- atomic publication of the entire site collection.

Legacy output may coexist on disk, but the new replay index must never ingest
it implicitly. Do not move, rename, or delete user archive data during this
change.

## 14. Validation and handoff completion

Before reporting completion:

1. Run the full deterministic test suite from `archive-magic-fetch/`.
2. Run the lockfile consistency check.
3. Run `git diff --check`.
4. Verify CLI help remains compatible.
5. Inspect a generated fixture collection tree.
6. Parse all fixture WARCs with `warcio`.
7. Validate every CDXJ offset/length against its WARC.
8. Confirm no routine `--<12 hex>` WARC suffix or `archives/urlkey/` path is
   generated.
9. Confirm the implementation never crosses into code outside
   `archive-magic-fetch/`.
10. Preserve unrelated dirty-worktree changes and do not stage or commit unless
    specifically requested.

A live Wayback smoke test is useful after deterministic validation if network
access is available, but it is not a substitute for the offline suite and
should not make CI depend on the Internet Archive.

## 15. Implementation principles

Apply KISS and YAGNI throughout:

- Use the high-level `wayback` client for discovery.
- Use the established CDXJ indexer if it is compatible.
- Keep source serialization small and explicit.
- Treat readable paths as storage organization, not record identity.
- Let WARC headers and CDXJ entries distinguish records.
- Group collisions instead of inventing filenames.
- Add only the two narrow artifact responsibilities the product now requires.

The key invariant is:

> A local filename may identify a storage bucket, but capture identity always
> comes from archive metadata.
