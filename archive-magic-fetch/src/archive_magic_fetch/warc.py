"""WARC 1.1 inventory, exact playback writing, and size-bounded rollover."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Mapping, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from warcio.archiveiterator import ArchiveIterator
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter
from wayback import Mode
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
)

from .collection import (
    ArchiveLayout,
    last_collection_warc,
    list_collection_partials,
    list_collection_warcs,
    parse_warc_partial_name,
    publish_file_atomically,
    warc_artifact_from_path,
)
from .models import (
    CDX_DIGEST_MATCH_HEADER,
    CDX_PAYLOAD_DIGEST_HEADER,
    CDX_STATUS_HEADER,
    CDX_URLKEY_HEADER,
    EMPTY_PAYLOAD_DIGEST,
    MISSING_CDX_PAYLOAD_DIGEST,
    MISSING_CDX_STATUS,
    SOFTWARE_ID,
    WARC_TARGET_BYTES,
    WARC_VERSION,
    CaptureIdentity,
    FailureCategory,
    PlaybackResult,
    RevisitResult,
    WarcArtifact,
    cdx_payload_digest_token,
    cdx_status_token,
    cdx_timestamp_to_warc_date,
    is_empty_payload_digest,
    is_redirect_status_token,
    make_identity,
    normalize_original_url,
    normalize_payload_digest,
    revisit_group_key,
    same_original_url,
    timestamp_to_warc_date,
    warc_date_to_cdx,
)


SLASH_REDIRECT_SOURCE_URI = "urn:archive-magic:slash-redirect"
_FOUND_CAPTURE_AT = re.compile(r"found capture at\s+\d+", re.IGNORECASE)
_WAYBACK_MEMENTO_LOCATION = re.compile(
    r"/web/\d{1,14}(?:id_|oe_|if_|tf_|fw_)?/(https?://.*)$",
    re.IGNORECASE,
)

_REPRESENTATION_HEADERS = {
    "content-digest",
    "content-encoding",
    "content-length",
    "content-md5",
    "digest",
    "etag",
    "repr-digest",
    "transfer-encoding",
}


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


def payload_digest(payload: bytes) -> str:
    """Return a CDX-compatible SHA-1 digest of payload bytes."""

    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def cdx_digest_matches_body(expected_digest: object, body: bytes) -> bool:
    """True when CDX digest matches the body, or body plus a trailing ``\\n``.

    Some early IA ARC indexes hashed ``payload + \"\\n\"`` while ``id_``
    playback returns the payload without that newline. Treat that as a match
    so the capture can seed revisits; still store the exact playback bytes.
    """

    expected = normalize_payload_digest(expected_digest)
    if expected is None:
        return True
    if payload_digest(body) == expected:
        return True
    return payload_digest(body + b"\n") == expected


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


def _content_type_from_cdx_mime(mime: str) -> str:
    if "/" in mime and "\r" not in mime and "\n" not in mime:
        return mime
    return "application/octet-stream"


def empty_http_200_from_cdx(
    identity: CaptureIdentity, *, mime: str
) -> PlaybackResult | None:
    """Materialize an HTTP 200 whose CDX digest is the empty payload.

    CDX already attested that the entity is zero bytes, so playback is skipped.
    Headers match payload-cache synthesis: CDX MIME and ``Content-Length: 0``.
    """

    if identity.status_token != "200":
        return None
    if not is_empty_payload_digest(identity.payload_digest):
        return None
    return PlaybackResult(
        identity=identity,
        body=b"",
        status_code=200,
        headers=(
            ("Content-Type", _content_type_from_cdx_mime(mime)),
            ("Content-Length", "0"),
        ),
        warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
        source_uri="urn:archive-magic:empty-payload",
        warc_payload_digest=EMPTY_PAYLOAD_DIGEST,
        digest_matched=True,
    )


def _is_unusable_playback_body(
    body: bytes,
    *,
    status_code: int,
    expected_digest: object = None,
) -> str | None:
    """Return a reason when IA served a non-content stub, else None.

    Historical redirects often have an empty entity with a ``Location``
    header. Empty non-redirect bodies are kept when CDX advertised no digest
    or the empty-payload digest; an empty body that contradicts a non-empty
    CDX digest is treated as a lost payload.
    """

    if not body:
        if is_redirect_status_token(str(status_code)):
            return None
        expected = normalize_payload_digest(expected_digest)
        if expected is None or is_empty_payload_digest(expected):
            return None
        return "empty playback body"
    stripped = body.strip()
    if stripped in {b"Invalid URI", b"Invalid URL"} or stripped.startswith(
        (b"Invalid URI", b"Invalid URL")
    ):
        return "IA playback stub: Invalid URI"
    return None


def _trailing_slash_url(url: str) -> str | None:
    """Return ``url`` with a trailing slash on the path, or None if it has one."""

    normalized = normalize_original_url(url)
    parts = urlsplit(normalized)
    if parts.path.endswith("/"):
        return None
    new_path = f"{parts.path}/" if parts.path else "/"
    return urlunsplit(
        (parts.scheme, parts.netloc, new_path, parts.query, parts.fragment)
    )


def _original_url_from_wayback_location(location: str) -> str | None:
    """Extract the original URL from a Wayback memento Location header."""

    if location.startswith("/"):
        candidate = location
    else:
        parts = urlsplit(location)
        candidate = parts.path
        if parts.query:
            candidate = f"{candidate}?{parts.query}"
        if parts.fragment:
            candidate = f"{candidate}#{parts.fragment}"
    match = _WAYBACK_MEMENTO_LOCATION.search(candidate)
    if match is None:
        return None
    return match.group(1)


def _found_capture_location(response) -> str | None:
    """Return an absolute memento URL from a ``found capture at …`` 302."""

    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    reason = headers.get("X-Archive-Redirect-Reason") or headers.get(
        "x-archive-redirect-reason"
    )
    if not isinstance(reason, str) or _FOUND_CAPTURE_AT.search(reason) is None:
        return None
    location = headers.get("Location") or headers.get("location")
    if not isinstance(location, str) or not location:
        return None
    if _original_url_from_wayback_location(location) is None:
        return None
    if location.startswith("/"):
        return urljoin("https://web.archive.org/", location)
    return location


def _slash_redirect_result(
    *,
    identity: CaptureIdentity,
    status_code: int,
    location_url: str,
) -> PlaybackResult:
    return PlaybackResult(
        identity=identity,
        body=b"",
        status_code=status_code,
        headers=(
            ("Location", location_url),
            ("Content-Length", "0"),
        ),
        warc_date=cdx_timestamp_to_warc_date(identity.timestamp),
        source_uri=SLASH_REDIRECT_SOURCE_URI,
        warc_payload_digest=EMPTY_PAYLOAD_DIGEST,
        digest_matched=True,
    )


def slash_redirect_from_cdx(
    identity: CaptureIdentity,
    *,
    group_urls: Sequence[str],
) -> PlaybackResult | None:
    """Materialize a slash-normalizing redirect from CDX without playback.

    SURT groups ``/path`` with ``/path/``. When CDX already listed the slash
    variant in this URL group and called this row a 301/302, Location is that
    slash URL. Wayback cannot play these captures exactly; asking it only adds
    a GET that commonly stalls on retry.
    """

    if not is_redirect_status_token(identity.status_token):
        return None
    slash_url = _trailing_slash_url(identity.original_url)
    if slash_url is None:
        return None
    if not any(same_original_url(other, slash_url) for other in group_urls):
        return None
    return _slash_redirect_result(
        identity=identity,
        status_code=int(identity.status_token),
        location_url=normalize_original_url(slash_url),
    )


def _slash_redirect_from_substitution(
    response,
    *,
    expected_url: str,
    expected_status: Optional[int],
    timestamp: str,
) -> PlaybackResult | None:
    """Rebuild a slash-normalizing 301/302 when Wayback will not play it exactly.

    Fallback when CDX did not list a slash sibling in this group. Wayback often
    answers exact ``id_`` playback of ``/path`` with a live 302
    ``found capture at …`` pointing at ``/path/``. That nearby capture is a
    different identity. If CDX called this row a redirect and the Location's
    original URL is this URL plus a trailing slash, store a redirect with that
    Location instead of treating the capture as unavailable.
    """

    if expected_status is None or not is_redirect_status_token(str(expected_status)):
        return None
    location = _found_capture_location(response)
    if location is None:
        return None
    target = _original_url_from_wayback_location(location)
    slash_url = _trailing_slash_url(expected_url)
    if target is None or slash_url is None:
        return None
    if not same_original_url(target, slash_url):
        return None
    return _slash_redirect_result(
        identity=make_identity(
            original_url=expected_url,
            timestamp=timestamp,
            status_token=str(expected_status),
            payload_digest=MISSING_CDX_PAYLOAD_DIGEST,
        ),
        status_code=expected_status,
        location_url=normalize_original_url(target),
    )


def _playback_from_memento(
    memento,
    *,
    expected_digest: object,
) -> tuple[bytes, int, str, object, tuple[tuple[str, str], ...], str]:
    with memento:
        body = memento.content
        status_code = memento.status_code
        memento_url = memento.memento_url
        memento_timestamp = memento.timestamp
        headers = tuple(
            _semantic_headers(memento.headers, len(body), status_code=status_code)
        )
        url = memento.url
    unusable = _is_unusable_playback_body(
        body, status_code=status_code, expected_digest=expected_digest
    )
    if unusable is not None:
        raise UnusablePlaybackError(unusable)
    return body, status_code, memento_url, memento_timestamp, headers, url


def _substituted_playback(
    client,
    response,
    *,
    expected_url: str,
    expected_status: Optional[int],
    timestamp: str,
    expected_digest: object,
) -> PlaybackResult | None:
    """Keep Wayback's nearby memento under the requested CDX identity.

    Exact ``id_`` playback often answers with ``found capture at …`` pointing at
    a SURT-equivalent URL or a different timestamp (session query strings,
    nearest capture). Prefer that inexact body over leaving a hole.
    """

    location = _found_capture_location(response)
    if location is None:
        return None
    try:
        memento = client.get_memento(
            location,
            mode=Mode.original,
            exact=True,
            follow_redirects=False,
        )
    except MementoPlaybackError:
        return None
    body, status_code, memento_url, _memento_timestamp, headers, _url = (
        _playback_from_memento(memento, expected_digest=expected_digest)
    )
    return PlaybackResult(
        identity=make_identity(
            original_url=expected_url,
            timestamp=timestamp,
            status_token=(
                str(expected_status)
                if expected_status is not None
                else MISSING_CDX_STATUS
            ),
            payload_digest=MISSING_CDX_PAYLOAD_DIGEST,
        ),
        body=body,
        status_code=status_code,
        headers=headers,
        warc_date=cdx_timestamp_to_warc_date(timestamp),
        source_uri=memento_url,
        warc_payload_digest=payload_digest(body),
        digest_matched=True,
        substituted=True,
    )


@contextlib.contextmanager
def _capture_first_session_response(client) -> Iterator[dict[str, object]]:
    """Stash the first ``session.request`` response from ``get_memento``."""

    stashed: dict[str, object] = {}
    session = getattr(client, "session", None)
    original = getattr(session, "request", None) if session is not None else None
    if not callable(original):
        yield stashed
        return
    def wrapped(method, url, **kwargs):
        response = original(method, url, **kwargs)
        stashed.setdefault("response", response)
        return response

    session.request = wrapped
    try:
        yield stashed
    finally:
        session.request = original


def download_exact(
    client,
    capture_url: str,
    timestamp: str,
    *,
    expected_status: Optional[int],
    expected_url: str,
    expected_digest: object = None,
) -> PlaybackResult:
    """Fetch one exact memento and return a validated playback result."""

    playback_error: BaseException | None = None
    stashed_response = None
    memento = None
    with _capture_first_session_response(client) as stashed:
        try:
            memento = client.get_memento(
                capture_url,
                timestamp=timestamp,
                mode=Mode.original,
                exact=True,
                follow_redirects=False,
            )
        except MementoPlaybackError as error:
            reconstructed = _slash_redirect_from_substitution(
                stashed.get("response"),
                expected_url=expected_url,
                expected_status=expected_status,
                timestamp=timestamp,
            )
            if reconstructed is not None:
                return reconstructed
            playback_error = error
            stashed_response = stashed.get("response")
    if playback_error is not None:
        substituted = _substituted_playback(
            client,
            stashed_response,
            expected_url=expected_url,
            expected_status=expected_status,
            timestamp=timestamp,
            expected_digest=expected_digest,
        )
        if substituted is not None:
            return substituted
        raise playback_error
    assert memento is not None
    body, status_code, memento_url, memento_timestamp, headers, url = (
        _playback_from_memento(memento, expected_digest=expected_digest)
    )

    returned_ts = timestamp_to_warc_date(memento_timestamp)
    returned_cdx = warc_date_to_cdx(returned_ts)
    if returned_cdx != timestamp:
        raise ExactMismatchError(
            f"timestamp mismatch: requested {timestamp}, got {returned_cdx}"
        )
    if not same_original_url(url, expected_url):
        raise ExactMismatchError(
            f"URL mismatch: requested {expected_url}, got {url}"
        )
    if expected_status is not None and status_code != expected_status:
        raise ExactMismatchError(
            f"status mismatch: requested {expected_status}, got {status_code}"
        )

    return PlaybackResult(
        identity=make_identity(
            original_url=expected_url,
            timestamp=timestamp,
            status_token=(
                str(expected_status)
                if expected_status is not None
                else MISSING_CDX_STATUS
            ),
            payload_digest=MISSING_CDX_PAYLOAD_DIGEST,
        ),
        body=body,
        status_code=status_code,
        headers=headers,
        warc_date=returned_ts,
        source_uri=memento_url,
        warc_payload_digest=payload_digest(body),
        digest_matched=True,
    )


def download_exact_for_identity(
    client,
    identity: CaptureIdentity,
) -> PlaybackResult:
    """Exact-playback one capture identity and attach full identity fields.

    A body that does not match the CDX digest (including the early-IA
    trailing-newline soft match) is kept for this capture but must not seed
    later revisit reuse. When exact playback cannot be played and Wayback
    answers with ``found capture at …``, the nearby memento is kept under this
    identity (inexact). Unusable stubs such as ``Invalid URI`` are always
    rejected. Empty bodies are kept for redirects, when CDX advertised the
    empty digest, or when CDX has no digest; an empty body that contradicts a
    non-empty CDX digest is rejected as a lost payload.
    """

    expected_status = (
        int(identity.status_token) if identity.status_token.isdigit() else None
    )
    result = download_exact(
        client,
        identity.original_url,
        identity.timestamp,
        expected_status=expected_status,
        expected_url=identity.original_url,
        expected_digest=identity.payload_digest,
    )
    actual_digest = result.warc_payload_digest
    digest_matched = (
        result.source_uri == SLASH_REDIRECT_SOURCE_URI
        or cdx_digest_matches_body(identity.payload_digest, result.body)
    )
    return PlaybackResult(
        identity=identity,
        body=result.body,
        status_code=result.status_code,
        headers=result.headers,
        warc_date=result.warc_date,
        source_uri=result.source_uri,
        warc_payload_digest=actual_digest,
        digest_matched=digest_matched,
        substituted=result.substituted,
    )


class ExactMismatchError(MementoPlaybackError):
    """Returned memento is not the requested capture."""


class UnusablePlaybackError(MementoPlaybackError):
    """IA returned a non-content stub (Invalid URI or empty-vs-CDX mismatch)."""


_RETRYABLE_HTTP_STATUSES = frozenset({429, *range(500, 600)})
_STATUS_IN_MESSAGE = re.compile(r"\b([45]\d\d)\b")


def classify_playback_error(error: BaseException) -> tuple[FailureCategory, bool]:
    """Return (category, retryable) for a playback error."""

    if isinstance(error, ExactMismatchError):
        return FailureCategory.EXACT_MISMATCH, False
    if isinstance(error, UnusablePlaybackError):
        return FailureCategory.UNAVAILABLE, False
    if isinstance(error, (BlockedByRobotsError, BlockedSiteError)):
        return FailureCategory.BLOCKED, False
    name = type(error).__name__
    # IA can store permanently truncated payloads whose advertised length is
    # larger than the bytes available. requests commonly wraps IncompleteRead
    # in ChunkedEncodingError and wayback wraps that again, so inspect the
    # complete outer message before generic connection-error classification.
    if (
        "IncompleteRead" in name
        or "Truncat" in name
        or "IncompleteRead" in str(error)
    ):
        return FailureCategory.TRUNCATED, False
    if "RateLimit" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    # Unwrap wayback's retry wrapper so connection/429 causes classify usefully.
    if "WaybackRetry" in name:
        nested = getattr(error, "cause", None)
        if isinstance(nested, BaseException):
            return classify_playback_error(nested)
        if isinstance(error.__cause__, BaseException):
            return classify_playback_error(error.__cause__)
        return FailureCategory.RETRY_EXHAUSTED, True
    if "Retryable" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    status = getattr(error, "status_code", None)
    if status is None:
        match = _STATUS_IN_MESSAGE.search(str(error))
        if match:
            status = int(match.group(1))
    if status in _RETRYABLE_HTTP_STATUSES:
        return FailureCategory.RETRY_EXHAUSTED, True
    if isinstance(error, MementoPlaybackError):
        return FailureCategory.UNAVAILABLE, False
    if "Timeout" in name or "Connection" in name or "Chunked" in name:
        return FailureCategory.RETRY_EXHAUSTED, True
    return FailureCategory.UNAVAILABLE, False


def _semantic_headers(
    headers: Mapping[str, str],
    payload_length: int,
    *,
    status_code: int,
) -> list[tuple[str, str]]:
    skip = set(_REPRESENTATION_HEADERS)
    # Preserve Content-Range for partial responses; the stored body is that
    # range and replay needs the header to describe it.
    if status_code != 206:
        skip.add("content-range")
    semantic = [
        (name, value)
        for name, value in headers.items()
        if name.lower() not in skip
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic


def _status_line(status_code: int) -> str:
    try:
        reason = HTTPStatus(status_code).phrase
    except ValueError:
        reason = ""
    return f"{status_code} {reason}".rstrip()


def build_response_record(result: PlaybackResult):
    """Create a WARC 1.1 response record for a playback result."""

    http_headers = StatusAndHeaders(
        _status_line(result.status_code),
        list(result.headers),
        protocol="HTTP/1.1",
    )
    warc_headers = {
        CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
        CDX_STATUS_HEADER: result.identity.status_token,
        CDX_URLKEY_HEADER: result.identity.urlkey,
        "WARC-Date": result.warc_date,
        "WARC-Source-URI": result.source_uri,
        "WARC-Payload-Digest": result.warc_payload_digest,
    }
    if not result.digest_matched:
        warc_headers[CDX_DIGEST_MATCH_HEADER] = "false"
    builder = RecordBuilder(warc_version=WARC_VERSION)
    return builder.create_warc_record(
        result.identity.original_url,
        "response",
        payload=BytesIO(result.body),
        length=len(result.body),
        http_headers=http_headers,
        warc_headers_dict=warc_headers,
    )


def build_revisit_record(result: RevisitResult):
    """Create a WARC 1.1 revisit record.

    Revisits store current capture identity via CDX extension headers and point
    at an earlier full response via ``WARC-Refers-To-*``. HTTP headers may be
    empty; pywb loads missing HTTP headers from the referenced response.
    """

    http_headers = StatusAndHeaders(
        _status_line(result.http_status_code),
        [],
        protocol="HTTP/1.1",
    )
    builder = RecordBuilder(warc_version=WARC_VERSION)
    return builder.create_warc_record(
        result.identity.original_url,
        "revisit",
        http_headers=http_headers,
        warc_headers_dict={
            CDX_PAYLOAD_DIGEST_HEADER: result.identity.payload_digest,
            CDX_STATUS_HEADER: result.identity.status_token,
            CDX_URLKEY_HEADER: result.identity.urlkey,
            "WARC-Date": result.warc_date,
            # Local digest of the referenced payload (may differ from CDX).
            "WARC-Payload-Digest": result.warc_payload_digest,
            "WARC-Profile": (
                "http://netpreserve.org/warc/1.1/revisit/identical-payload-digest"
            ),
            "WARC-Refers-To-Target-URI": result.refers_to_target_uri,
            "WARC-Refers-To-Date": result.refers_to_date,
        },
    )


@dataclass(frozen=True)
class SalvagedWarc:
    """One in-progress WARC promoted to a finalized collection shard."""

    collection_id: str
    sequence: int
    path: Path
    record_count: int


def truncate_incomplete_gzip_warc(path: Path) -> int | None:
    """Keep complete gzip members; return record count or None if unusable."""

    if not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        return None
    good_end = 0
    count = 0
    first_type: str | None = None
    try:
        with path.open("rb") as stream:
            iterator = ArchiveIterator(stream, check_digests="raise")
            for record in iterator:
                if first_type is None:
                    first_type = record.rec_type
                record.raw_stream.read()
                count += 1
                good_end = (
                    iterator.get_record_offset() + iterator.get_record_length()
                )
    except Exception:  # noqa: BLE001 - torn gzip member is expected
        pass
    if first_type != "warcinfo" or count < 2 or good_end <= 0:
        path.unlink(missing_ok=True)
        return None
    size = path.stat().st_size
    if good_end < size:
        os.truncate(path, good_end)
    return count


def salvage_collection_partials(layout: ArchiveLayout) -> list[SalvagedWarc]:
    """Promote visible and leftover hidden WARC partials into finalized shards."""

    salvaged: list[SalvagedWarc] = []
    pending: list[tuple[str, int, Path]] = []

    if layout.work_root.is_dir():
        for path in layout.work_root.iterdir():
            if not path.is_file():
                continue
            parsed = _parse_legacy_work_partial(layout, path.name)
            if parsed is None:
                continue
            collection_id, sequence = parsed
            pending.append((collection_id, sequence, path))

    if layout.collections_root.is_dir():
        for collection_dir in sorted(layout.collections_root.iterdir()):
            if not collection_dir.is_dir():
                continue
            try:
                collection_id = layout.validate_collection_id(collection_dir.name)
            except ValueError:
                continue
            for path in list_collection_partials(layout, collection_id):
                sequence = parse_warc_partial_name(
                    layout, collection_id, path.name
                )
                if sequence is None:
                    continue
                pending.append((collection_id, sequence, path))

    for collection_id, sequence, path in pending:
        artifact = _publish_salvaged_partial(
            layout, collection_id, sequence, path
        )
        if artifact is not None:
            salvaged.append(artifact)

    if layout.work_root.is_dir():
        try:
            remaining = list(layout.work_root.iterdir())
        except OSError:
            remaining = [layout.work_root]
        if not remaining:
            shutil.rmtree(layout.work_root, ignore_errors=True)

    return salvaged


def _parse_legacy_work_partial(
    layout: ArchiveLayout, name: str
) -> tuple[str, int] | None:
    """Parse `.tmp-*.{archive}-{collection}-{seq}.warc.gz.partial` names."""

    if not name.endswith(".warc.gz.partial"):
        return None
    if not layout.collections_root.is_dir():
        match = re.fullmatch(
            rf"^(?:\.tmp-[^.]+\.)?{re.escape(layout.archive_id)}-"
            rf"(?P<collection>[A-Za-z0-9][A-Za-z0-9._-]*)-"
            rf"(?P<seq>\d{{3}})\.warc\.gz\.partial$",
            name,
        )
        if match is None:
            return None
        return match.group("collection"), int(match.group("seq"))
    for collection_dir in layout.collections_root.iterdir():
        if not collection_dir.is_dir():
            continue
        try:
            collection_id = layout.validate_collection_id(collection_dir.name)
        except ValueError:
            continue
        sequence = parse_warc_partial_name(layout, collection_id, name)
        if sequence is not None:
            return collection_id, sequence
    match = re.fullmatch(
        rf"^(?:\.tmp-[^.]+\.)?{re.escape(layout.archive_id)}-"
        rf"(?P<collection>[A-Za-z0-9][A-Za-z0-9._-]*)-"
        rf"(?P<seq>\d{{3}})\.warc\.gz\.partial$",
        name,
    )
    if match is None:
        return None
    return match.group("collection"), int(match.group("seq"))


def _publish_salvaged_partial(
    layout: ArchiveLayout,
    collection_id: str,
    sequence: int,
    path: Path,
) -> SalvagedWarc | None:
    """Truncate a partial and replace the shard only when it is an improvement."""

    record_count = truncate_incomplete_gzip_warc(path)
    if record_count is None:
        return None
    try:
        validate_warc(path)
    except Exception:  # noqa: BLE001 - unusable leftover
        path.unlink(missing_ok=True)
        return None
    final_path = layout.collection_warc_path(collection_id, sequence)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.is_file() and path.stat().st_size <= final_path.stat().st_size:
        path.unlink(missing_ok=True)
        return None
    publish_file_atomically(path, final_path)
    return SalvagedWarc(
        collection_id=collection_id,
        sequence=sequence,
        path=final_path,
        record_count=record_count,
    )


@dataclass
class CollectionWarcWriter:
    """Single-owner WARC writer for one portable collection."""

    layout: ArchiveLayout
    collection_id: str
    target_bytes: int = WARC_TARGET_BYTES
    sequence: int = 0
    stream: BinaryIO | None = None
    writer: WARCWriter | None = None
    temp_path: Path | None = None
    record_count: int = 0
    finalized: list[WarcArtifact] = field(default_factory=list)
    _continue_from: Path | None = None

    def __post_init__(self) -> None:
        if self.sequence != 0:
            return
        last = last_collection_warc(self.layout, self.collection_id)
        if last is None:
            self.sequence = 1
            return
        sequence, path = last
        if path.stat().st_size < self.target_bytes:
            self.sequence = sequence
            self._continue_from = path
            return
        nxt = sequence + 1
        if nxt > 999:
            raise RuntimeError(
                f"WARC sequence would exceed 999 for collection {self.collection_id}"
            )
        self.sequence = nxt

    def write_playback(self, result: PlaybackResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_response_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._flush()
        self._maybe_rotate()

    def write_revisit(self, result: RevisitResult) -> None:
        self._ensure_open()
        assert self.writer is not None
        record = build_revisit_record(result)
        self.writer.write_record(record)
        self.record_count += 1
        self._flush()
        self._maybe_rotate()

    def close(self) -> list[WarcArtifact]:
        """Finalize any open shard and return all newly published WARCs."""

        if self.stream is not None or self.temp_path is not None:
            self._finalize_current()
        return list(self.finalized)

    def _flush(self) -> None:
        if self.stream is not None:
            self.stream.flush()

    def _ensure_open(self) -> None:
        if self.writer is not None:
            return
        if self.sequence > 999:
            raise RuntimeError(
                f"WARC sequence would exceed 999 for collection {self.collection_id}"
            )
        collection_dir = self.layout.collection_dir(self.collection_id)
        collection_dir.mkdir(parents=True, exist_ok=True)
        partial = self.layout.collection_warc_partial_path(
            self.collection_id, self.sequence
        )
        final_name = self.layout.collection_warc_filename(
            self.collection_id, self.sequence
        )
        continue_from = self._continue_from
        self._continue_from = None
        if continue_from is not None and continue_from.is_file():
            shutil.copyfile(continue_from, partial)
            self.stream = partial.open("ab")
            self.temp_path = partial
            self.writer = WARCWriter(
                self.stream,
                gzip=True,
                warc_version=WARC_VERSION,
            )
            self.record_count = 0
            return
        self.temp_path = partial
        self.stream = partial.open("xb")
        self.writer = WARCWriter(
            self.stream,
            gzip=True,
            warc_version=WARC_VERSION,
        )
        warcinfo = self.writer.create_warcinfo_record(
            final_name,
            {
                "software": SOFTWARE_ID,
                "format": f"WARC File Format {WARC_VERSION}",
            },
        )
        self.writer.write_record(warcinfo)
        self._flush()
        self.record_count = 1  # warcinfo counts as a record for size only

    def _maybe_rotate(self) -> None:
        assert self.temp_path is not None
        if self.temp_path.stat().st_size < self.target_bytes:
            return
        self._finalize_current()

    def _finalize_current(self) -> None:
        assert self.temp_path is not None
        if self.stream is not None:
            try:
                self.stream.flush()
            except OSError:
                pass
            try:
                self.stream.close()
            except OSError:
                pass
        self.stream = None
        self.writer = None
        record_count = truncate_incomplete_gzip_warc(self.temp_path)
        if record_count is None:
            self.temp_path = None
            self.record_count = 0
            return
        validate_warc(self.temp_path)
        final_path = self.layout.collection_warc_path(
            self.collection_id, self.sequence
        )
        publish_file_atomically(self.temp_path, final_path)
        artifact = warc_artifact_from_path(
            self.layout,
            final_path,
            record_count=count_warc_records(final_path),
        )
        self.finalized.append(artifact)
        self.temp_path = None
        self.record_count = 0
        self.sequence += 1
        self._continue_from = None


def validate_warc(path: Path) -> None:
    """Require one fully parseable WARC with valid digests."""

    types: list[str] = []
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests="raise"):
            types.append(record.rec_type)
            record.raw_stream.read()
    if not types or types[0] != "warcinfo":
        raise ValueError(f"WARC missing leading warcinfo: {path}")


def count_warc_records(path: Path) -> int:
    """Return the number of records in a finalized WARC."""

    count = 0
    with path.open("rb") as stream:
        for record in ArchiveIterator(stream, check_digests=False):
            count += 1
            record.raw_stream.read()
    return count


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
