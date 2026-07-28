# Wayback content-decoding failures: investigation handoff

Date: 2026-07-27

## Purpose

This memo summarizes the remaining Wayback playback failures after the retry
and connection-pool changes, with emphasis on:

```text
Received response with content-encoding: gzip, but failed to decode it.
Error -3 while decompressing data: incorrect header check
```

It records what is known, distinguishes safe-looking from unsafe-looking
payloads, and outlines potential recovery policies. It intentionally does not
propose specific code changes.

## Executive summary

The connection changes solved the largest delay problem. The latest
single-worker run completed in 23.2 minutes, compared with 51.3 minutes in the
preceding run. Decoding failures no longer retry or incur exponential
backoff.

The 241 decoding failures are not 241 equivalent corrupt gzip streams. In
representative raw replay checks, Wayback declared `Content-Encoding: gzip`
but returned bytes that started directly with HTML rather than the gzip magic
bytes `1f 8b`.

Those raw responses separated into at least two classes:

1. The raw HTML exactly matched the capture's CDX SHA-1 payload digest. This
   strongly suggests a stale or contradictory encoding header around an
   otherwise faithful archived payload.
2. The raw HTML did not match the CDX digest and ended in the middle of an HTML
   element. This strongly suggests a transformed or clipped replay body. It
   must not be accepted merely by removing `Content-Encoding`.

The CDX digest therefore appears to be the most useful conservative recovery
gate. A digest match establishes fidelity to the archived payload. It does
not establish that the original crawl captured a semantically complete
document.

File extensions are not a safe format signal. All 241 failed captures have
CDX status `200` and MIME type `text/html`, including the three URLs ending in
`.pdf`. The representative `.pdf` replay returned HTML, not PDF bytes.

## Evidence from the latest run

Source artifacts:

- [Complete log](../../archives/wecanstopthehate.org/sources/20260727T071850.283092Z/log.txt)
- [CDX capture list](../../archives/wecanstopthehate.org/sources/20260727T071850.283092Z/captures.cdx.gz)
- [Query parameters](../../archives/wecanstopthehate.org/sources/20260727T071850.283092Z/query.json)

### Run result

| Result | Count |
| --- | ---: |
| Selected captures | 6,327 |
| Responses written | 3,234 |
| Revisits written | 2,531 |
| Redirects omitted | 300 |
| Playback failures | 262 |
| Invalid content encoding | 241 |
| Repeatedly truncated response | 11 |
| Other playback failures | 10 |

Invalid content encoding accounts for 92.0% of playback failures and 3.8% of
all selected captures.

The run logged 28 transport retry lines across 12 captures, representing 136
seconds of scheduled delay:

| Retry cause | Lines | Captures | Scheduled delay |
| --- | ---: | ---: | ---: |
| Repeated `IncompleteRead` | 23 | 11 | 110 seconds |
| Connection refused | 5 | 2 | 26 seconds |

No decoding failure was retried. Connection-refusal delay fell from 1,252
scheduled seconds in the preceding run to 26 seconds in this run.

### Decoding-failure concentration

All 241 warnings contain the same nested Requests/zlib error. They are
concentrated in a few historical crawl windows:

| Capture date | Failures |
| --- | ---: |
| 2009-01-07 | 77 |
| 2008-05-16 | 64 |
| 2008-09-05 | 33 |
| 2008-11-21 | 23 |
| 2008-11-19 | 15 |
| 2008-07-04 | 12 |
| Other dates | 17 |

The six largest dates contain 224 of 241 failures (92.9%). The largest hourly
clusters are 2008-05-16 18:00 (41 failures) and 2009-01-07 11:00 (28
failures).

The affected URL families are similarly concentrated:

| First path component | Failures |
| --- | ---: |
| `site` | 102 |
| `outrage` | 49 |
| `flashpoints` | 42 |
| `videos` | 18 |
| `factions` | 9 |
| Other | 21 |

This clustering points to a capture-batch or imported-record issue rather than
intermittent client decompression failures.

### CDX metadata

All 241 failed captures were matched to their CDX rows:

- CDX status: `200` for 241 of 241.
- CDX MIME type: `text/html` for 241 of 241.
- Unique CDX payload digests: 206.
- URL extension: 238 have no extension and three end in `.pdf`.
- The three `.pdf` rows are nevertheless labeled `text/html`.

Only four failed captures share a CDX digest with a capture that played
successfully during this run. Reusing an equivalent payload from another
capture may therefore help a few cases, but it is not a general solution.

## What the warning currently means

Playback responses are streamed and closed through the Memento context. The
HTTP client normally materializes the semantic response body and automatically
decodes any declared `Content-Encoding`. When Requests sees
`Content-Encoding: gzip`, it invokes gzip decoding. If the first bytes are
plain HTML, zlib raises `incorrect header check`.

The current policy treats that exception as a representation failure:

- no retry;
- no `Accept-Encoding: identity` fallback;
- no connection-pool reset;
- one warning and one categorized playback failure;
- no WARC record for that capture.

This policy is safe and fast, but it discards both truly unsafe replays and
potentially recoverable payloads.

`Accept-Encoding: identity` is ineffective for these records because the
problematic `Content-Encoding` is archived origin metadata replayed by
Wayback, not fresh content negotiation. A representative identity request
returned the same `Content-Encoding: gzip` header and the same plain body.

## Representative raw-replay findings

The following checks requested the exact `id_` replay while reading raw bytes
without HTTP-client decompression.

### Digest-matching examples

The capture:

<https://web.archive.org/web/20090107113930id_/http://www.wecanstopthehate.org/site/page/uploads/file_REAL_ID_summary_for_Latinos_final_bill.pdf>

returned:

- `Content-Type: text/html`;
- `Content-Encoding: gzip`;
- a 2,466-byte body beginning with whitespace and `<!DOCTYPE`;
- no gzip magic bytes;
- raw-body SHA-1 Base32
  `ZJIWIKFAP6NGTRRXRIDDVTOP4JOOEOI5`, exactly matching its CDX digest.

The `.pdf` suffix therefore describes the requested route, not the stored
payload format. This capture contains archived HTML.

The capture:

<https://web.archive.org/web/20081119142249id_/http://www.wecanstopthehate.org/outrage/the_myth_of_widespread_noncitizen_voting>

also returned plain HTML whose raw-body digest exactly matched its CDX digest.

### Digest-mismatching example

The capture:

<https://web.archive.org/web/20080511204035id_/http://www.wecanstopthehate.org/allies/>

returned:

- `Content-Type: text/html`;
- `Content-Encoding: gzip`;
- a stable 3,278-byte plain-HTML body;
- no gzip magic bytes;
- a body ending mid-element at `<h1 class=`;
- raw-body digest `ODWQ7Y42KB3QUBB6O5HZ55FWFY6JYCTS`;
- CDX digest `DK7NC4LQBQZUYPRK747WYC6GFZBCL3ZA`.

The current replay `Content-Length` and archived-origin content length were
both 3,278, yet the digest mismatch and mid-element ending show that length
agreement alone is not a sufficient integrity check. One plausible
explanation is that a decoded representation was clipped at a stale compressed
content-length boundary.

### Small cross-cluster sample

Thirteen distinct failed captures were checked across the major historical
clusters:

- seven raw bodies matched their CDX payload digest;
- six raw bodies did not;
- all started as plain HTML rather than gzip data.

Some digest-matching bodies still ended in apparently incomplete markup. A CDX
digest match proves that the bytes agree with the archived payload represented
by the index; it cannot prove that the crawler originally captured a complete
document.

Several representative responses expose
`X-Archive-Orig-X_Commoncrawl-*` headers and an ARC source through
`X-Archive-Src`. This supports, but does not prove, the hypothesis that these
are imported Common Crawl records with inconsistent stored representation
metadata.

## Recovery requirements

Any recovery policy should preserve these properties:

1. **Fidelity:** do not silently write bytes that differ from the selected
   archived payload.
2. **Valid replay:** do not retain a `gzip` content-encoding header around
   plain bytes.
3. **Exact provenance:** preserve the selected capture timestamp and source
   URI; do not silently substitute a nearby capture.
4. **Format independence:** treat URL extensions as hints at most. Prefer
   response metadata, byte signatures, and integrity evidence.
5. **Bounded cost:** a malformed replay must not restore long retry or backoff
   delays.
6. **Auditability:** distinguish recovered captures from discarded captures
   in summaries and provenance.
7. **Conservatism:** an uncertain payload remains a failure rather than being
   promoted to a successful WARC record.

## Potential solution directions

### 1. Digest-gated raw recovery

This is the smallest promising policy.

Conceptually, the replay would be evaluated as raw representation bytes before
automatic content decoding. The declared encoding, byte signature,
decompression result, transfer completeness, and CDX payload digest would
determine whether the bytes are trustworthy.

For the failure pattern observed here:

- If a replay declares gzip but the body is plainly uncompressed and its
  SHA-1 matches the CDX payload digest, treat the body as the faithful archived
  payload with a stale encoding header.
- If the raw digest does not match, discard it or escalate to a stronger source
  recovery method.
- If the raw body actually has gzip magic, require successful, complete gzip
  decompression and validate the resulting payload against the CDX digest.

An accepted semantic payload would need valid semantic headers and a newly
computed length and WARC payload digest. Representation headers that describe
the invalid transfer cannot remain attached to plain bytes.

Advantages:

- grounded in archive-supplied integrity metadata rather than content guessing;
- handles HTML, PDF, images, and other formats uniformly;
- no repeated request is inherently required;
- cleanly rejects the clipped replay example.

Limitations and questions:

- CDX digest semantics should be verified across collections and replay modes;
- a digest match proves archived-byte fidelity, not document completeness;
- the current library boundary may expose the decoding exception before the
  caller can retain raw bytes and headers;
- recovery reporting and provenance need a clear policy.

### 2. Retrieve the underlying ARC/WARC source record

For digest-mismatching replays, the strongest recovery would bypass the altered
playback representation and read the underlying archived record. Potential
leads include the `X-Archive-Src` value and CDX fields for source filename,
offset, and record length.

Advantages:

- may recover the exact stored HTTP record, including the original encoded
  entity and headers;
- can distinguish a replay-layer clipping defect from a genuinely truncated
  source capture.

Limitations:

- substantially more operational and format complexity;
- source files or byte ranges may not be publicly accessible;
- imported ARC/Common Crawl records may require collection-specific handling;
- likely conflicts with KISS/YAGNI unless the digest-mismatch population is
  both large and valuable.

This should be researched before it is treated as a product direction.

### 3. Reuse an equivalent payload from another successful capture

When another capture has the same CDX payload digest and plays successfully,
its semantic body is a candidate source for the failed capture's identical
payload.

Advantages:

- integrity is supported by the shared archive digest;
- avoids interpreting malformed representation bytes.

Limitations:

- applies to only four of 241 failures in this run;
- provenance must make clear that payload bytes were obtained through another
  replay;
- response headers may differ even when the payload digest is identical.

This is a narrow optimization, not the primary recovery strategy.

### 4. Explicit substitution with a nearby capture

A user-selected policy could substitute the nearest successful capture of the
same URL when exact-capture recovery fails.

This improves site-level coverage but is not exact archival recovery. It must
never happen silently, and substituted captures should retain their real
timestamps and provenance. This direction is better treated as a separate
collection policy than as content-decoding recovery.

### 5. Continue strict discard

The current policy remains the correct fallback whenever integrity cannot be
established. It is simple, bounded, and prevents malformed or clipped bytes
from entering the collection.

## Approaches that are insufficient by themselves

- **Trusting the URL extension.** The three `.pdf` failures are CDX
  `text/html`, and the representative body is HTML.
- **Sending `Accept-Encoding: identity`.** Wayback still replays the archived
  gzip declaration.
- **Removing `Content-Encoding` blindly.** This would accept the clipped
  `allies/` body as successful.
- **Trusting `Content-Length`.** The clipped `allies/` response had exact
  length agreement.
- **Retrying the same replay.** The problem is deterministic for these
  captures and retrying reintroduces delay without adding integrity evidence.
- **Accepting any recognizable HTML/PDF/image signature.** A recognizable
  prefix can still be truncated or transformed.
- **Treating curl/browser display as proof.** A client that does not request or
  perform compression may expose readable bytes without proving that they are
  the complete archived payload.

## Suggested next investigation

The next pass can remain diagnostic and policy-focused:

1. Fetch raw bytes once for all 241 failed captures with conservative pacing.
2. Record declared encoding, content type, byte signature, raw length,
   transfer completeness, raw digest, CDX digest, and whether the payload ends
   cleanly for its apparent format.
3. Quantify the digest-match and digest-mismatch populations by crawl date and
   source ARC.
4. Validate CDX digest semantics against known successful captures and any
   genuinely gzip-encoded examples.
5. Determine whether source filename/offset metadata and the underlying
   ARC/WARC bytes are publicly retrievable for digest mismatches.
6. Decide whether the first recovery policy should accept only
   digest-verified plain payloads and leave every mismatch as a warning.

A useful decision table is:

| Declared gzip | Raw gzip magic | Processing result | CDX digest | Candidate outcome |
| --- | --- | --- | --- | --- |
| Yes | No | Plain bytes | Match | Recover as faithful semantic payload |
| Yes | No | Plain bytes | Mismatch | Discard or inspect source record |
| Yes | Yes | Complete decompression | Match | Recover decoded payload |
| Yes | Yes | Complete decompression | Mismatch | Discard or inspect source record |
| Yes | Yes | Decompression fails | Any | Discard or inspect source record |

The first row appears capable of recovering a meaningful fraction of this
run's failures without format-specific rules. The second row is the critical
guard against writing clipped data.

## Adjacent failures outside this memo's main scope

The latest run also has:

- 11 PDF captures repeatedly ending at 130,808 or 130,810 received bytes;
- 10 generic unplayable Mementos, all involving `favicon.ico` or `robots.txt`.

The fixed truncation boundary is a separate transport or replay-source issue
and should not be folded into invalid-gzip recovery. The repeated-boundary
rule permits one retry, then stops after two consecutive attempts reach the
same received/expected byte counts. The incomplete-response retry is immediate
and does not use exponential backoff. Intervening transport errors reset the
consecutive-boundary count and can still extend a mixed failure sequence.

## Relevant implementation areas

For orientation only:

- [Playback retrieval and failure categorization](../src/archive_magic_fetch/retrieval.py)
- [Wayback session and transport retry policy](../src/archive_magic_fetch/retry.py)
- [WARC export failure handling](../src/archive_magic_fetch/export.py)
- [Current fetch architecture](ARCHITECTURE-FETCH.md)

The core unresolved policy question is not “How can the client ignore gzip?”
It is “What evidence is sufficient to promote raw replay bytes to a faithful
semantic archived payload?” The current evidence supports a CDX-digest gate as
the first answer to investigate.
