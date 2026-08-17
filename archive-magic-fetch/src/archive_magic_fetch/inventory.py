"""CDXJ-backed capture inventory and identical-payload revisits."""

from __future__ import annotations

from dataclasses import dataclass, field

from .collection import ArchiveLayout
from .identity import (
    cdx_payload_digest_token,
    cdx_status_token,
    cdx_timestamp_to_warc_date,
    make_identity,
    normalize_payload_digest,
    revisit_group_key,
    warc_date_to_cdx,
)
from .index import parse_cdxj_line
from .models import CaptureIdentity, PlaybackResult, RevisitResult
from .protocol import (
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


@dataclass
class CollectionInventory:
    """Exact captures for one year and reusable responses for revisits.

    ``identities`` are this year's captures. ``by_url_digest`` maps a
    revisit group key to the oldest matched full response. Fetch may seed
    earlier years' representatives. Entries store compact locator metadata
    only (never payloads).
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
        point forward.
        """

        key = revisit_group_key(
            CaptureIdentity(
                urlkey=urlkey,
                original_url="",
                timestamp=not_after_timestamp,
                status_token=status_token,
                payload_digest=ia_digest,
            )
        )
        if key is None:
            return None
        stored = self.by_url_digest.get(key)
        if stored is None:
            return None
        if stored.identity.timestamp > not_after_timestamp:
            return None
        return stored

    def remember_representative(self, stored: StoredResponse) -> None:
        """Record a successful full response for later revisit short-circuits.

        Keeps the oldest representative for each revisit group key.
        Callers must pass only successfully written, digest-matched
        responses. Missing IA digests cannot group.
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
    """Rebuild capture identity from Archive Magic WARC headers."""

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
        raise ValueError(f"WARC record has invalid {CDX_PAYLOAD_DIGEST_HEADER}")
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
    """Build exact capture and revisit inventory from the collection CDXJ."""

    inv = CollectionInventory()
    index_path = layout.collection_index(collection_id)
    if not index_path.is_file():
        return inv
    with index_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                urlkey, timestamp, meta = parse_cdxj_line(line)
                identity = make_identity(
                    original_url=str(meta["url"]),
                    timestamp=timestamp,
                    status_token=str(meta.get("cdxStatus", meta.get("status", "-"))),
                    payload_digest=str(meta["cdxDigest"]),
                    urlkey=str(meta.get("cdxUrlkey", urlkey)),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"{index_path}, line {number}: missing capture identity metadata"
                ) from error
            inv.identities.add(identity)
            if meta.get("mime") == "warc/revisit":
                continue
            warc_payload = normalize_payload_digest(meta.get("digest"))
            cdx_payload = normalize_payload_digest(identity.payload_digest)
            if warc_payload is None or cdx_payload is None:
                continue
            if meta.get("cdxDigestMatch") is not True:
                continue
            status_code = (
                int(identity.status_token) if identity.status_token.isdigit() else 200
            )
            inv.remember_representative(
                StoredResponse(
                    identity=identity,
                    warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
                    warc_payload_digest=warc_payload,
                    target_uri=identity.original_url,
                    status_code=status_code,
                )
            )
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
