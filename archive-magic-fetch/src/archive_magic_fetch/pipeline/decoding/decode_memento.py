"""Decode and validate an exact Wayback capture."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from wayback import Memento

from archive_magic_fetch.adapters.interpret_wayback_failures import (
    ExactMismatchError,
    UnusablePlaybackError,
)
from archive_magic_fetch.archive.identity import (
    is_empty_payload_digest,
    is_redirect_status_token,
    normalize_original_url,
    normalize_payload_digest,
    payload_digest,
    timestamp_to_warc_date,
    warc_date_to_cdx,
)
from archive_magic_fetch.models import CaptureRef, CaptureResult

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


def decode_memento(memento: Memento, capture: CaptureRef) -> CaptureResult:
    """Fetch and validate one exact capture identity."""

    identity = capture.identity
    expected_status = (
        int(identity.status_token) if identity.status_token.isdigit() else None
    )
    body, status_code, memento_url, memento_timestamp, headers, url = (
        _playback_from_memento(memento, expected_digest=identity.payload_digest)
    )
    returned_ts = timestamp_to_warc_date(memento_timestamp)
    returned_cdx = warc_date_to_cdx(returned_ts)
    if returned_cdx != identity.timestamp:
        raise ExactMismatchError(
            f"timestamp mismatch: requested {identity.timestamp}, got {returned_cdx}"
        )
    if not _same_original_url(url, identity.original_url):
        raise ExactMismatchError(
            f"URL mismatch: requested {identity.original_url}, got {url}"
        )
    if expected_status is not None and status_code != expected_status:
        raise ExactMismatchError(
            f"status mismatch: requested {expected_status}, got {status_code}"
        )
    return CaptureResult(
        identity=identity,
        body=body,
        status_code=status_code,
        headers=headers,
        warc_date=returned_ts,
        source_uri=memento_url,
        warc_payload_digest=payload_digest(body),
        digest_matched=_cdx_digest_matches_body(identity.payload_digest, body),
    )


def _cdx_digest_matches_body(expected_digest: object, body: bytes) -> bool:
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


def _semantic_headers(
    headers: Mapping[str, str],
    payload_length: int,
    *,
    status_code: int,
) -> list[tuple[str, str]]:
    skip = set(_REPRESENTATION_HEADERS)
    if status_code != 206:
        skip.add("content-range")
    semantic = [
        (name, value) for name, value in headers.items() if name.lower() not in skip
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic


def _same_original_url(left: str, right: str) -> bool:
    """Compare original URLs while tolerating IA percent-encoding variants."""

    a = normalize_original_url(left)
    b = normalize_original_url(right)
    if a == b:
        return True
    a_parts = urlsplit(a)
    b_parts = urlsplit(b)
    if (a_parts.scheme, a_parts.netloc) != (b_parts.scheme, b_parts.netloc):
        return False
    a_resource = tuple(
        map(_fully_unquote, (a_parts.path, a_parts.query, a_parts.fragment))
    )
    b_resource = tuple(
        map(_fully_unquote, (b_parts.path, b_parts.query, b_parts.fragment))
    )
    return a_resource == b_resource


def _fully_unquote(value: str) -> str:
    previous = None
    current = value
    while previous != current:
        previous = current
        current = unquote(current)
    return current
