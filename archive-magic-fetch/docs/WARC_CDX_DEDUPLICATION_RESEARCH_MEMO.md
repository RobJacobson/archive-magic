# WARC, CDX, Deduplication, and Replay Research Memo

**Status:** Historical research summary; non-normative · **Architecture decision superseded:** 2026-07-22

**Date:** 2026-07-20

**Last verified:** 2026-07-20 against `cdx_toolkit` 0.9.39, pywb 2.9.1, and the current IIPC WARC specifications

**Purpose:** Summarize research into WARC 1.0 and 1.1, CDX/CDXJ, `cdx_toolkit`, revisit-based deduplication, and pywb interoperability, and evaluate the implementation options behind the current Archive Magic architecture.

This memo is non-normative. If it conflicts with `ARCHITECTURE-FETCH.md`, the current architecture controls.

> **Decision update (2026-07-22):** The current MVP intentionally groups one
> WARC per CDX URL key and deduplicates payload/status combinations across URL
> spellings in that group. It writes cross-target canonical reference fields in
> WARC 1.0 and accepts the interoperability tradeoff described below. The older
> same-exact-URL recommendation retained in this research memo is historical,
> not an implementation requirement.

## Executive summary

WARC 1.0 and WARC 1.1 use the same basic container model and the same eight core record types. WARC 1.0 already supports storage-saving `revisit` records; WARC 1.1 primarily improves timestamps, URI grammar, and the standardized way a revisit identifies an earlier payload, particularly when the original payload was captured at a different URL.

pywb can index and replay both WARC 1.0 and WARC 1.1. Its established WARC 1.0 resolution path joins captures of the same URL by payload digest through CDX. Its cross-URL revisit path uses the WARC 1.1 fields `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date` to perform a second index lookup.

`cdx_toolkit` is well suited to discovering captures and retrieving Internet Archive playback or Common Crawl records. The current upstream release is 0.9.39. Its stock WARC exporter remains incomplete for the required export policy: it targets WARC 1.0 by default, warns that other versions are not correctly supported, resolves Internet Archive revisits into synthesized response records, and explicitly lists smarter revisit generation as unfinished.

A two-pass design in which `cdx_toolkit` first writes an intermediate WARC and Archive Magic later rewrites it is feasible. It would provide a durable acquisition cache, but it would download and temporarily store every duplicate, require the original CDX inventory to repair lost or transformed semantics, and still require normalization, deduplication, final WARC writing, CDXJ generation, and validation. It moves the necessary exporter work rather than eliminating it.

The strongest default, now adopted by `ARCHITECTURE.md`, is:

1. Pin and import unmodified upstream `cdx_toolkit` for CDX discovery and normalization only; do not maintain an Archive Magic fork.
2. Put public archived-content retrieval, payload verification, same-exact-URL deduplication, and final WARC writing in a separately testable core inside `archive-magic-fetch`.
3. Treat the selected CDX inventory as authoritative for capture identity, timestamp, status, MIME type, digest, and redirect metadata.
4. Process each exact target URL independently, using a fresh digest map for that URL and creating one WARC 1.0 file for it.
5. Retrieve and write the first occurrence of a digest as a full response; write a later same-URL occurrence as a revisit without downloading it when the CDX row provides enough semantics.
6. Hash every downloaded payload and fail the collection when its valid source digest does not match; do not add a second actual-digest deduplication path.
7. Let the Fetch application build the inventory, CDXJ, manifest, validation, and atomic publication transaction around the closed WARCs.
8. Keep Replay separate and connected only through completed collection files.

WARC 1.0 is a reasonable simplification if Archive Magic deliberately limits deduplication to captures of the same exact target URL. WARC 1.1 remains the better fit if collection-wide, cross-URL deduplication is a product requirement. The current first-release architecture selects the WARC 1.0 same-exact-URL profile and leaves WARC 1.1 cross-URL deduplication as an optional later feature.

## 1. The roles of WARC and CDX

WARC and CDX solve different layers of the archive problem:

- **WARC stores archived records.** A response record normally contains WARC headers, an archived HTTP response status and headers, and the response body.
- **CDX or CDXJ indexes captures.** It summarizes a capture and records where the corresponding WARC record can be retrieved.
- **pywb combines them.** It queries CDX, range-loads the selected WARC record, resolves revisit dependencies, and rewrites the archived response for replay.

A useful shorthand is that WARC is the storage stack and CDX is its catalog. CDX is deliberately lossy; it is not a substitute for WARC.

Typical CDXJ fields map to WARC or embedded HTTP information as follows:

| CDXJ field | Meaning or source |
| --- | --- |
| `urlkey` | Canonicalized/SURT lookup key derived from the target URL |
| `timestamp` | Capture date normalized to `YYYYMMDDhhmmss` |
| `url` | `WARC-Target-URI` |
| `status` | Status from the archived HTTP response |
| `mime` | MIME type of the response payload, not the WARC record block |
| `digest` | Usually the payload digest corresponding to `WARC-Payload-Digest` |
| `filename` | WARC file containing the record |
| `offset` | Compressed byte offset of the record |
| `length` | Compressed byte range required to retrieve the record |

Two distinctions matter operationally:

- A response WARC record commonly has `Content-Type: application/http; msgtype=response`, while its CDX MIME type might be `text/html` or `image/jpeg`.
- WARC `Content-Length` is the size of the uncompressed record block. CDXJ `length` normally describes the compressed record range.

Record-at-a-time GZIP makes CDX range retrieval practical: every WARC record is an independently compressed GZIP member, so `filename + offset + length` can retrieve one record without decompressing everything before it.

## 2. WARC 1.0 and WARC 1.1

WARC 1.1 is an incremental revision, not a new archive design.

| Area | WARC 1.0 | WARC 1.1 |
| --- | --- | --- |
| Record marker | `WARC/1.0` | `WARC/1.1` |
| Core record types | Eight | The same eight |
| Capture dates | Whole-second UTC | Variable W3C/ISO-8601 precision, including fractional seconds |
| URI grammar | Accidentally implied angle brackets around all URI values | Bare ordinary URIs; explicit angle brackets for record-ID references |
| Revisit source | Primarily `WARC-Refers-To` record ID | Adds original target URI and capture date |
| Cross-URL duplicate | Not explicitly standardized | Explicitly allowed by the identical-payload profile |
| Revisit payload model | Contains contradictory general wording | Explicitly recognizes the logical payload represented by a revisit |
| Supporting references | Older Base32 and IPv6 RFCs | Updated RFC references |

Both versions support extensions and require readers to ignore unknown fields and record types. Both use the same core record framing and mandatory fields.

### 2.1 URI grammar

WARC 1.0's grammar defined `uri` as including angle brackets even though common implementations and examples did not consistently put them around target URLs or profile URIs. WARC 1.1 aligns the grammar with practice:

```text
WARC-Record-ID: <urn:uuid:...>
WARC-Refers-To: <urn:uuid:...>
WARC-Target-URI: https://example.org/image.jpg
WARC-Profile: http://netpreserve.org/warc/1.1/revisit/identical-payload-digest
```

### 2.2 Capture-date precision

WARC 1.0 uses whole-second timestamps such as:

```text
WARC-Date: 2026-07-20T18:30:00Z
```

WARC 1.1 also permits fractional or reduced precision. Traditional CDX timestamps remain fourteen digits, so fractional precision is normally lost during indexing. Archive Magic already chooses whole-second capture dates, so this WARC 1.1 capability does not currently provide a product benefit.

## 3. Deduplication through revisit records

WARC 1.0 already defines `revisit` records and two standard profiles:

- identical payload digest;
- server not modified.

The first capture stores the complete payload. A later identical capture can store a small revisit record containing its capture metadata and payload digest without repeating the body.

For example, an 8 MB image captured 100 times can occupy roughly one complete 8 MB response plus 99 small revisit records instead of approximately 800 MB.

### 3.1 Deduplication granularity

Standard revisit deduplication applies to a complete payload. It does not normally find common chunks inside different payloads.

| Capture situation | Normal revisit outcome |
| --- | --- |
| Same external JPEG repeatedly captured | Deduplicable |
| Same JPEG body with different HTTP headers | Deduplicable by payload digest |
| Same bytes served at a different URL | Explicitly supported by WARC 1.1 |
| Image embedded in changing HTML as a data URI | Not independently deduplicable |
| PDF changes by one page | Entire payload differs; not deduplicable |
| JavaScript bundle changes by a few bytes | Entire payload differs; not deduplicable |

Web pages usually reference images, stylesheets, scripts, and fonts as separate HTTP resources. Those resources therefore receive separate WARC records and are good candidates for revisit deduplication.

### 3.2 WARC 1.0 same-URL resolution

A conventional WARC 1.0 revisit can contain:

```text
WARC/1.0
WARC-Type: revisit
WARC-Target-URI: https://example.org/hero.jpg
WARC-Profile: http://netpreserve.org/warc/1.0/revisit/identical-payload-digest
WARC-Payload-Digest: sha1:...
WARC-Refers-To: <urn:uuid:original-record>
Content-Length: 0
```

For captures of the same URL, pywb can join CDX entries with matching digests and attach the earlier payload location to the revisit entry. Replay then uses the current revisit headers and earlier response body.

Although `WARC-Refers-To` is valuable archival metadata, ordinary CDXJ does not generally provide an efficient record-ID index. The same-URL digest join, not a record-ID lookup, is therefore the important pywb resolution path.

### 3.3 WARC 1.1 cross-URL resolution

WARC 1.1 adds:

```text
WARC-Refers-To-Target-URI: https://example.org/original.jpg
WARC-Refers-To-Date: 2026-01-01T12:00:00Z
```

When pywb cannot join a revisit to an earlier capture of the same URL, it reads these fields and performs another CDX lookup for the original URL and date, filtered by digest. This supports a collection-wide digest map in which a body first seen at one URL becomes canonical for the same body later seen at another.

Without those fields, a cross-URL revisit containing only `WARC-Refers-To` is not reliably replayable through normal pywb CDXJ lookup.

## 4. pywb support

pywb supports WARC 1.0, WARC 1.1, and legacy ARC input. Its underlying `warcio` library explicitly supports reading both WARC ISO versions.

Relevant replay behavior includes:

- ordinary response replay from one WARC record;
- same-URL revisit resolution through CDX digest joins;
- loading headers from the revisit and the body from the canonical response;
- cross-URL revisit resolution through `WARC-Refers-To-Target-URI` and `WARC-Refers-To-Date`;
- failure when a required canonical payload cannot be found.

The WARC version is therefore not a general pywb compatibility blocker. The meaningful choice is the revisit lookup contract Archive Magic wants to support.

## 5. `cdx_toolkit` behavior

`cdx_toolkit` 0.9.39, released June 1, 2026 and still the current stable release at the verification date, provides a useful abstraction over the Common Crawl and Internet Archive CDX APIs. It supports paged iteration, time ranges, filters, capture content retrieval, and WARC extraction. Those are the upstream built-in sources. Archive Magic's integration remains archive-neutral for other public CDX endpoints that are compatible with the pinned stock client, while Fetch supplies explicit public playback or public-WARC retrieval adapters for every source it claims to support.

### 5.1 Common Crawl

For Common Crawl, a CDX row supplies a WARC filename, compressed offset, and compressed length. `cdx_toolkit` range-fetches that member and parses the source record. It adds source URI/range provenance to the in-memory record.

### 5.2 Internet Archive

For Internet Archive, `cdx_toolkit` retrieves Wayback playback and synthesizes a new response record. It does not retrieve an original raw WARC record.

Consequences include:

- a source revisit can be materialized or "vivified" into a complete response;
- playback status can differ from the selected CDX status;
- redirect handling is reconstructed from playback and indexed information;
- the synthesized `WARC-Date` can be derived from the archived HTTP `Date` header rather than always from the selected capture timestamp;
- the emitted record is a useful reconstruction, but the original CDX row must remain authoritative for the selected capture's identity and timeline.

The stock `cdxt warc` command writes these fetched records into WARC files. It warns when a revisit is being resolved. Its writer defaults to WARC 1.0 and its source explicitly reports that versions other than 1.0 are not correctly supported. The project README also lists smarter revisit generation as unfinished.

These limitations do not make `cdx_toolkit` unsuitable for CDX discovery. They do mean that Archive Magic does not use the stock `cdxt warc` command as its final exporter. The Fetch core consumes discovered CDX rows and owns retrieval, verification, deduplication, and final WARC writing. Archive Magic-specific inventory files, manifests, publication, and CLI behavior remain in the Fetch application layer outside that core.

## 6. Evaluated implementation options

### Option A: Optional WARC 1.1 collection-wide profile

```text
CDX inventory
    -> skip safe expected-digest hits
    -> fetch first-seen payload
    -> normalize and verify
    -> write response or WARC 1.1 revisit
    -> finalize WARC
    -> generate CDXJ
```

**Advantages**

- Avoids downloading most duplicate payloads.
- Avoids temporarily storing duplicate bodies.
- Supports same-URL and cross-URL deduplication.
- Produces standardized, self-describing cross-URL revisit references.
- Requires one final WARC-writing pass.

**Costs**

- Requires enhanced response/revisit construction in the Fetch core.
- Must preserve compact capture semantics when duplicate payloads are skipped.
- Must validate canonical references and pywb replay behavior.

This remains a useful future Fetch profile, but it is not the current first-release Archive Magic profile.

### Option B: WARC 1.0 with same-exact-URL deduplication (adopted)

Process one exact target URL at a time and use a fresh canonical map keyed by payload digest:

```text
digest -> canonical response within the current exact URL
```

The URL is the surrounding processing and file boundary, so it does not need to be repeated in the key. The map is discarded when that URL's WARC closes. CDX `urlkey` or SURT normalization must not merge exact target URLs.

**Advantages**

- pywb's most established revisit resolution path.
- Broad compatibility with older WARC tools.
- Retains most savings for stable resource URLs.
- `warcio` creates WARC 1.0 records by default.

**Costs**

- Same bytes at different URLs must be stored as separate full responses.
- Reduces savings for aliases, CDN URLs, changing query strings, and other cross-URL duplicates.

This is the current first-release decision. It is a deliberate compatibility and scope choice, not an assertion that WARC 1.1 is unsupported or undesirable.

### Option C: Stock `cdx_toolkit` WARC followed by a rewrite pass

```text
cdxt warc
    -> intermediate full-response WARC
    -> join records back to source inventory
    -> repair capture semantics
    -> compute/verify digests
    -> rewrite responses/revisits
    -> generate new CDXJ
```

**Advantages**

- Creates a durable acquisition cache.
- Separates network retrieval from final archive policy.
- Allows repeated experiments without contacting the source again.
- Makes fetched reconstructions directly inspectable.

**Costs**

- Downloads every duplicate before deduplicating it.
- Temporarily stores both the expanded and compact archives.
- Requires an additional complete read/write pass.
- Cannot rely on the intermediate WARC alone; the source CDX inventory is still needed to restore authoritative timestamp, status, redirect, and source revisit information.
- Still requires the complete normalization and enhanced exporter logic.
- Invalidates every intermediate CDX offset; final CDXJ must be regenerated.

This option is justified when a durable raw acquisition cache is itself a requirement. It is not a simpler route to the final artifact.

### Option D: Maintained-fork record-boundary exporter (not adopted)

Use `cdx_toolkit` capture objects as the input to a separately testable exporter component within the maintained fork, then write only the final record:

```text
capture = CDX row
record = fork fetches through playback or public WARC range, only when needed
normalized = fork normalizes and verifies(capture, record)
output = fork chooses response or revisit
fork writes final record with warcio
```

This is best understood as post-processing at the record boundary rather than post-processing an already persisted WARC. It avoids expanded intermediate archives and a second write pass, but assigns CDX discovery, content retrieval, policy, and WARC serialization to one maintained dependency. The current architecture rejects that ownership boundary because Archive Magic can use the stock client for CDX and keep its specialized export pipeline in Fetch.

### Option E: Stock CDX client plus Fetch-core exporter (adopted)

Use stock `cdx_toolkit` only to obtain CDX rows, then pass those rows to a separately testable Fetch core:

```text
capture = stock cdx_toolkit returns CDX row
decision = Fetch core checks the current URL's digest map
record = Fetch core retrieves through playback or public WARC range when needed
verified = Fetch core hashes downloaded bytes and enforces source digest
output = Fetch core writes WARC 1.0 response or same-URL revisit with warcio
```

This preserves one final write pass and avoids downloading duplicate payloads after a verified canonical response exists for the current URL. It also creates clear internal stages without requiring a fork: stock `cdx_toolkit` owns CDX protocol behavior, the Fetch core owns source retrieval and WARC policy, and the Fetch application owns the collection transaction. The cost is that Fetch maintains the relatively small retrieval and record-writing layer itself.

## 7. Findings and recommendation

The research supports the following findings:

1. **WARC 1.0 is capable of meaningful deduplication.** WARC 1.1 is not required merely to omit duplicate payloads.
2. **pywb compatibility does not require WARC 1.1.** pywb replays both versions.
3. **The material WARC-version decision is deduplication scope.** Strict WARC 1.0 naturally fits same-URL digest joining; WARC 1.1 provides the standard metadata pywb uses for cross-URL lookup.
4. **Upstream `cdx_toolkit` is valuable for CDX discovery, but its stock final WARC exporter does not implement the required policy.** Archive Magic can consume the stock discovery API without adopting that exporter or maintaining a fork.
5. **Full-WARC post-processing is feasible but is not inherently simpler.** It trades network and temporary disk efficiency for a durable intermediate acquisition artifact.
6. **The source inventory must remain authoritative.** Internet Archive playback is a reconstruction and can transform revisit, date, status, and redirect representation.
7. **CDXJ must describe final bytes.** Any rewrite or recompression requires regenerating indexes after the final WARC closes.

The recommended implementation boundary is Option E with the WARC 1.0 profile from Option B:

- stock, exactly pinned `cdx_toolkit` completes CDX discovery and returns the selected rows;
- the Fetch application durably preserves that inventory and confirms export;
- the Fetch core groups captures by exact target URL and opens one WARC per URL;
- the Fetch core avoids fetching later same-URL duplicate-digest captures when it can create a valid revisit from CDX metadata;
- the Fetch core retrieves other captures through playback or public WARC ranges, hashes the payload, and treats a valid source-digest mismatch as fatal;
- the Fetch core writes each final WARC 1.0 response or revisit once with `warcio` and discards the URL's digest map when the WARC closes;
- the Fetch application generates and validates CDXJ, inventory, checksums, and the manifest after final WARCs close; and
- Replay remains a separate project that consumes only completed collection files.

The WARC profiles remain an explicit scope choice:

- The first Archive Magic release uses **WARC 1.0** with same-exact-URL deduplication.
- A future Fetch profile may add **WARC 1.1**, multiple URLs per WARC, and collection-wide cross-URL deduplication after interoperability tests pass.

Selecting WARC 1.0 does not make stock `cdxt warc` sufficient. Capture preservation, redirect and timestamp correctness, strict downloaded-payload verification, response/revisit generation, self-contained subset export, and structured outcomes remain Fetch-core responsibilities. Collection validation and publication remain Fetch-application responsibilities.

## 8. Primary references

- [IIPC WARC 1.0 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.0/)
- [IIPC WARC 1.1 specification](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1/)
- [IIPC WARC 1.1 with community annotations](https://iipc.github.io/warc-specifications/specifications/warc-format/warc-1.1-annotated/)
- [IIPC CDX format, 2015 proposal](https://iipc.github.io/warc-specifications/specifications/cdx-format/cdx-2015/)
- [Common Crawl CDXJ documentation](https://commoncrawl.org/cdxj-index)
- [`cdx_toolkit` 0.9.39 on PyPI](https://pypi.org/project/cdx-toolkit/0.9.39/)
- [`cdx_toolkit` 0.9.39 release source](https://github.com/commoncrawl/cdx_toolkit/tree/72f37d653b41eb088f58aaf50d84e6c576f03eaf)
- [`cdx_toolkit` README](https://github.com/commoncrawl/cdx_toolkit/blob/main/README.md)
- [`cdx_toolkit` WARC implementation](https://github.com/commoncrawl/cdx_toolkit/blob/main/cdx_toolkit/warc.py)
- [`cdx_toolkit` WARC CLI path](https://github.com/commoncrawl/cdx_toolkit/blob/main/cdx_toolkit/cli.py)
- [`warcio` documentation](https://warcio.readthedocs.io/en/latest/)
- [pywb 2.9.1 on PyPI](https://pypi.org/project/pywb/2.9.1/)
- [pywb record lookup and revisit resolution](https://github.com/webrecorder/pywb/wiki/PyWb-Record-Lookup-and-Revisits)
- [pywb Warcserver documentation](https://pywb.readthedocs.io/en/latest/manual/warcserver.html)

## 9. Relationship to the current architecture

- [`ARCHITECTURE.md`](ARCHITECTURE.md) records the current project architecture and controls when it differs from this research memo.
