"""Collection inventory and cross-collection payload reuse."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from warcio.archiveiterator import ArchiveIterator

from .collection import ArchiveLayout, list_collection_warcs
from .identity import (
    cdx_payload_digest_token,
    cdx_status_token,
    cdx_timestamp_to_warc_date,
    make_identity,
    normalize_payload_digest,
    revisit_group_key,
    warc_date_to_cdx,
)
from .models import CaptureIdentity, PlaybackResult, RevisitResult
from .playback import (
    _content_type_from_cdx_mime,
    cdx_digest_matches_body,
    payload_digest,
)
from .policy import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
    MISSING_CDX_PAYLOAD_DIGEST,
)

@dataclass(frozen=True)
class StoredResponse:
    """Compact revisit reference for one full response.

    Never retain payload bytes or HTTP headers; pywb resolves them from the
    referenced full response.
    """

    identity: CaptureIdentity
    warc_date: str
    warc_payload_digest: str
    target_uri: str
    status_code: int


@dataclass(frozen=True)
class PayloadLocator:
    """Indexed location of one reusable full response payload."""

    collection_id: str
    warc_path: Path
    offset: int
    length: int
    timestamp: str


@dataclass
class PriorPayloadCache:
    """Best-effort IA-digest lookup over finalized collection responses."""

    by_ia_digest: dict[str, PayloadLocator] = field(default_factory=dict)

    @classmethod
    def from_layout(cls, layout: ArchiveLayout) -> "PriorPayloadCache":
        cache = cls()
        if not layout.collections_root.is_dir():
            return cache
        for collection_dir in sorted(layout.collections_root.iterdir()):
            if collection_dir.is_dir():
                cache.add_collection(layout, collection_dir.name)
        return cache

    def add_collection(self, layout: ArchiveLayout, collection_id: str) -> None:
        """Add eligible full responses from one collection's portable index."""

        collection_id = layout.validate_collection_id(collection_id)
        index_path = layout.collection_index(collection_id)
        if not index_path.is_file():
            return
        with index_path.open(encoding="utf-8") as stream:
            for line in stream:
                locator_and_digest = _payload_locator_from_cdxj_line(
                    layout, collection_id, line
                )
                if locator_and_digest is None:
                    continue
                ia_digest, locator = locator_and_digest
                existing = self.by_ia_digest.get(ia_digest)
                if existing is None or (
                    _payload_locator_order(locator)
                    < _payload_locator_order(existing)
                ):
                    self.by_ia_digest[ia_digest] = locator

    def materialize(
        self,
        identity: CaptureIdentity,
        *,
        mime: str,
        current_collection_id: str,
    ) -> PlaybackResult | None:
        """Return a current response copied from an earlier cached payload."""

        ia_digest = normalize_payload_digest(identity.payload_digest)
        if ia_digest is None or identity.status_token != "200":
            return None
        locator = self.by_ia_digest.get(ia_digest)
        if locator is None or locator.collection_id == current_collection_id:
            return None
        if locator.timestamp > identity.timestamp:
            return None
        try:
            record, body = _read_indexed_response(locator, check_digests="raise")
            source_identity = get_warc_identity(record)
            if source_identity.payload_digest != ia_digest:
                return None
            if source_identity.timestamp != locator.timestamp:
                return None
            if source_identity.timestamp > identity.timestamp:
                return None
            if record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER) == "false":
                return None
            if not cdx_digest_matches_body(ia_digest, body):
                return None
            source_status = int(record.http_headers.get_statuscode())
            if source_status != 200:
                return None
            actual_digest = payload_digest(body)
            stored_digest = normalize_payload_digest(
                record.rec_headers.get_header("WARC-Payload-Digest")
            )
            if stored_digest != actual_digest:
                return None
            return PlaybackResult(
                identity=identity,
                body=body,
                status_code=200,
                headers=(
                    ("Content-Type", _content_type_from_cdx_mime(mime)),
                    ("Content-Length", str(len(body))),
                ),
                warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
                source_uri=f"urn:archive-magic:payload-cache:{ia_digest}",
                warc_payload_digest=actual_digest,
                digest_matched=True,
            )
        except Exception:  # noqa: BLE001 - cache corruption is always a miss
            return None


def _payload_locator_order(locator: PayloadLocator) -> tuple[str, str, str, int]:
    return (
        locator.timestamp,
        locator.collection_id,
        locator.warc_path.name,
        locator.offset,
    )


def _read_indexed_response(
    locator: PayloadLocator,
    *,
    check_digests: bool | str,
):
    size = locator.warc_path.stat().st_size
    if locator.offset < 0 or locator.length <= 0:
        raise ValueError("invalid cached WARC byte range")
    if locator.offset + locator.length > size:
        raise ValueError("cached WARC byte range is out of bounds")
    with locator.warc_path.open("rb") as stream:
        stream.seek(locator.offset)
        encoded = stream.read(locator.length)
    if len(encoded) != locator.length:
        raise EOFError("cached WARC byte range is truncated")
    iterator = ArchiveIterator(BytesIO(encoded), check_digests=check_digests)
    record = next(iterator)
    if record.rec_type != "response" or record.http_headers is None:
        raise ValueError("cached WARC record is not a full response")
    body = record.content_stream().read()
    try:
        next(iterator)
    except StopIteration:
        pass
    else:
        raise ValueError("cached WARC byte range contains multiple records")
    return record, body


def _payload_locator_from_cdxj_line(
    layout: ArchiveLayout,
    collection_id: str,
    line: str,
) -> tuple[str, PayloadLocator] | None:
    try:
        parts = line.split(" ", 2)
        if len(parts) != 3:
            return None
        timestamp = parts[1]
        if len(timestamp) != 14 or not timestamp.isdigit():
            return None
        meta = json.loads(parts[2])
        if meta.get("mime") == "warc/revisit":
            return None
        filename = meta.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        offset = int(meta.get("offset"))
        length = int(meta.get("length"))
        status_token = str(meta.get("status", ""))
        if status_token != "200":
            return None
        warc_path = layout.collection_dir(collection_id) / filename
        locator = PayloadLocator(
            collection_id=collection_id,
            warc_path=warc_path,
            offset=offset,
            length=length,
            timestamp=timestamp,
        )
        ia_digest = normalize_payload_digest(meta.get("cdxDigest"))
        if ia_digest is not None and meta.get("cdxDigestMatch") is True:
            return ia_digest, locator
        return None
    except Exception:  # noqa: BLE001 - cache hydration is best effort
        return None


@dataclass
class CollectionInventory:
    """Exact captures and reusable responses from one portable collection.

    ``by_url_digest`` maps ``(urlkey, IA/CDX payload digest, CDX status)`` to
    the oldest matched full response with that key. Entries store compact
    locator metadata only (never payloads), rebuilt from finalized collection
    WARCs on resume.
    """

    identities: set[CaptureIdentity] = field(default_factory=set)
    by_url_digest: dict[tuple[str, str, str], StoredResponse] = field(
        default_factory=dict
    )

    def contains(self, identity: CaptureIdentity) -> bool:
        return identity in self.identities

    def lookup_representative(
        self,
        urlkey: str,
        ia_digest: str,
        status_token: str,
        *,
        not_after_timestamp: str,
    ) -> StoredResponse | None:
        """Return a prior successful response usable for a capture timestamp.

        Reject representatives after the capture timestamp so revisits never
        point forward within the year.
        """

        if ia_digest == MISSING_CDX_PAYLOAD_DIGEST:
            return None
        stored = self.by_url_digest.get((urlkey, ia_digest, status_token))
        if stored is None:
            return None
        if stored.identity.timestamp > not_after_timestamp:
            return None
        return stored

    def remember_representative(self, stored: StoredResponse) -> None:
        """Record a successful full response for later revisit short-circuits.

        Keeps the oldest representative for each
        ``(urlkey, IA digest, CDX status)``. Callers must pass only
        successfully written, digest-matched responses. Missing IA digests
        cannot group.
        """

        key = revisit_group_key(stored.identity)
        if key is None:
            return
        existing = self.by_url_digest.get(key)
        if (
            existing is None
            or stored.identity.timestamp < existing.identity.timestamp
        ):
            self.by_url_digest[key] = stored



def get_warc_identity(record) -> CaptureIdentity:
    """Rebuild capture identity from WARC extension headers."""

    target_uri = record.rec_headers.get_header("WARC-Target-URI")
    warc_date = record.rec_headers.get_header("WARC-Date")
    cdx_digest = record.rec_headers.get_header(CDX_PAYLOAD_DIGEST_HEADER)
    cdx_status = record.rec_headers.get_header(CDX_STATUS_HEADER)
    cdx_urlkey = record.rec_headers.get_header(CDX_URLKEY_HEADER)
    if not target_uri or not warc_date:
        raise ValueError("WARC record is missing target URI or date")
    if cdx_digest is None:
        raise ValueError(f"WARC record is missing {CDX_PAYLOAD_DIGEST_HEADER}")
    if cdx_status is None:
        raise ValueError(f"WARC record is missing {CDX_STATUS_HEADER}")
    digest_token = cdx_payload_digest_token(cdx_digest)
    if (
        normalize_payload_digest(cdx_digest) is None
        and cdx_digest.strip() != MISSING_CDX_PAYLOAD_DIGEST
    ):
        raise ValueError(
            f"WARC record has invalid {CDX_PAYLOAD_DIGEST_HEADER}"
        )
    return make_identity(
        original_url=target_uri,
        timestamp=warc_date_to_cdx(warc_date),
        status_token=cdx_status_token(cdx_status),
        payload_digest=digest_token,
        urlkey=cdx_urlkey or None,
    )


def inventory_collection(
    layout: ArchiveLayout, collection_id: str
) -> CollectionInventory:
    """Validate and inventory finalized WARCs for one collection.

    Finalized collection WARCs are the recovery source of truth. Rebuild exact
    identity membership and the compact representative map without loading
    payload bodies.
    """

    inv = CollectionInventory()
    for path in list_collection_warcs(layout, collection_id):
        with path.open("rb") as stream:
            for record in ArchiveIterator(stream, check_digests="raise"):
                if record.rec_type not in {"response", "revisit"}:
                    record.raw_stream.read()
                    continue
                identity = get_warc_identity(record)
                inv.identities.add(identity)
                warc_payload = normalize_payload_digest(
                    record.rec_headers.get_header("WARC-Payload-Digest")
                )
                if record.rec_type == "response" and warc_payload is not None:
                    status_code = 200
                    try:
                        status_code = int(record.http_headers.get_statuscode())
                    except (TypeError, ValueError, AttributeError):
                        if identity.status_token.isdigit():
                            status_code = int(identity.status_token)
                    # Compact inventory: no body, no HTTP headers.
                    stored = StoredResponse(
                        identity=identity,
                        warc_date=record.rec_headers.get_header("WARC-Date"),
                        warc_payload_digest=warc_payload,
                        target_uri=identity.original_url,
                        status_code=status_code,
                    )
                    cdx_payload = normalize_payload_digest(identity.payload_digest)
                    explicitly_mismatched = (
                        record.rec_headers.get_header(CDX_DIGEST_MATCH_HEADER)
                        == "false"
                    )
                    # Exact matches have equal digests. Soft matches (IA CDX
                    # hashed body+"\n") differ but omit CDX-Digest-Match:false
                    # and must still seed revisits after resume.
                    if not explicitly_mismatched and cdx_payload is not None:
                        inv.remember_representative(stored)
                # consume body stream
                record.raw_stream.read()
    return inv

def revisit_from_stored(
    identity: CaptureIdentity,
    stored: StoredResponse,
) -> RevisitResult:
    """Build a revisit referencing an earlier successful full response.

    Prefer the current capture's CDX status for the HTTP status line when it is
    numeric. Pywb fills omitted HTTP headers from the referred response.
    """

    http_status_code = (
        int(identity.status_token)
        if identity.status_token.isdigit()
        else stored.status_code
    )
    return RevisitResult(
        identity=identity,
        warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
        refers_to_target_uri=stored.target_uri,
        refers_to_date=stored.warc_date,
        warc_payload_digest=stored.warc_payload_digest,
        http_status_code=http_status_code,
    )


def stored_from_playback(result: PlaybackResult) -> StoredResponse:
    """Create compact inventory metadata for a just-written full response."""

    return StoredResponse(
        identity=result.identity,
        warc_date=result.warc_date,
        warc_payload_digest=result.warc_payload_digest,
        target_uri=result.identity.original_url,
        status_code=result.status_code,
    )
