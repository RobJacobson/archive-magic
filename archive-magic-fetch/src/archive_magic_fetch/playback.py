"""Wayback sessions and exact capture playback."""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

import requests
from wayback import Mode, WaybackClient, WaybackSession
from wayback._client import read_and_close
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    RateLimitError,
)

from .identity import (
    is_empty_payload_digest,
    is_redirect_status_token,
    normalize_payload_digest,
    same_original_url,
    timestamp_to_warc_date,
    warc_date_to_cdx,
)
from .models import CaptureIdentity, FailureCategory, PlaybackResult
from .request_stats import PlaybackHTTPAdapter, PlaybackRequestStats
from .retry import parse_retry_after


_REPRESENTATION_HEADERS = {
    "content-digest", "content-encoding", "content-length", "content-md5",
    "digest", "etag", "repr-digest", "transfer-encoding",
}
_GZIP_MAGIC = b"\x1f\x8b"


class ArchiveMagicWaybackSession(WaybackSession):
    """Wayback session tuned for Archive Magic fetch.

    Playback clients leave library retries disabled because Fetch owns their
    synchronous retry loop. CDX clients also leave library retries disabled;
    `fetch_cdx` owns CDX retries: transient 504s, rate limits, and wall-clock
    timeouts each use a distinct pause and attempt budget.

    Wayback treats any response with ``Memento-Datetime`` as a successful
    memento, which can let HTTP 429 slip through as a playback error with no
    ``retry_after``. Always surface 429 as ``RateLimitError`` and carry an
    explicit `Retry-After` value when IA supplied one.

    Some memento responses also advertise ``Content-Encoding: gzip`` while the
    transfer body is already plaintext (for example HTML starting with
    ``<!DOCTYPE``). ``requests`` then raises ``ContentDecodingError`` when
    reading ``.content``. This session forces ``stream=True``, and for mementos
    that claim gzip it reads the raw body and only decompresses when the gzip
    magic is present.
    """

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("retries", 0)
        super().__init__(*args, **kwargs)

    def track_playback(self, stats: PlaybackRequestStats) -> None:
        """Apply one shared gate and counter at the HTTP transport boundary."""

        self.retries = 0
        for prefix in ("https://", "http://"):
            self.adapters[prefix].close()
            self.mount(prefix, PlaybackHTTPAdapter(stats))

    def send(self, request, **kwargs):
        # requests.Session.send() eagerly reads ``response.content`` unless
        # stream=True. That triggers ContentDecodingError on IA's false gzip
        # claims before we can inspect the raw body, so defer loading for
        # memento URLs only. CDX responses are not mementos; leave them
        # non-streaming so the body download stays under the session timeout.
        raw_url = getattr(request, "url", None)
        path = urlsplit(raw_url if isinstance(raw_url, str) else "").path
        if path.startswith("/web/"):
            kwargs["stream"] = True
        response = super().send(request, **kwargs)
        if getattr(response, "status_code", None) == 429:
            delay = parse_retry_after(response.headers.get("Retry-After"))
            read_and_close(response)
            raise RateLimitError(response, delay)
        repair_false_gzip_content_encoding(response)
        return response


def repair_false_gzip_content_encoding(response: requests.Response) -> None:
    """Decode memento bodies that falsely claim ``Content-Encoding: gzip``.

    Edge case: Internet Archive occasionally returns a memento with
    ``Content-Encoding: gzip`` whose on-the-wire body is already uncompressed
    (magic bytes are HTML/PDF/etc., not ``\\x1f\\x8b``). urllib3/requests then
    fail with ``ContentDecodingError`` ("incorrect header check").

    Callers must obtain the response with ``stream=True`` (the session
    ``send()`` override does this) so ``requests`` has not already attempted
    content decoding.

    Only memento responses (those with ``Memento-Datetime``) are rewritten, and
    only when they claim gzip. CDX entity downloads keep streaming with
    ``decode_content=False`` and must not have their bodies eagerly consumed
    here. After repair, ``Content-Encoding`` is removed and ``response.content``
    is the logical payload (decompressed when the body was real gzip).

    Mismatched usable bodies are kept for that capture only.
    """

    headers = getattr(response, "headers", None)
    if headers is None or "Memento-Datetime" not in headers:
        return

    encoding = (headers.get("Content-Encoding") or "").split(",")[0].strip().lower()
    if encoding not in {"gzip", "x-gzip"}:
        return

    # Already materialized (for example by a prior hook); do not re-read.
    if getattr(response, "_content", False) is not False:
        return

    raw_stream = getattr(response, "raw", None)
    if raw_stream is None:
        return

    # Disable urllib3's content-decoder so we can inspect the true payload.
    if hasattr(raw_stream, "decode_content"):
        raw_stream.decode_content = False
    raw = raw_stream.read()
    if raw.startswith(_GZIP_MAGIC):
        try:
            body = gzip.decompress(raw)
        except OSError:
            # Truncated or corrupt gzip: keep bytes for caller classification.
            body = raw
    else:
        # False Content-Encoding: IA claimed gzip but sent plaintext. Keep the
        # bytes so the caller can compare them with the CDX digest and retain
        # the response without treating it as reusable when they disagree.
        body = raw

    # Body is now the logical entity; drop the misleading transfer coding.
    try:
        del response.headers["Content-Encoding"]
    except KeyError:
        pass
    response._content = body
    response._content_consumed = True


def make_client() -> WaybackClient:
    """Return a playback client paced by Archive Magic's shared gate."""

    return WaybackClient(
        session=ArchiveMagicWaybackSession(
            user_agent="archive-magic-fetch",
            memento_calls_per_second=0,
        )
    )

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


def download_exact(client, identity: CaptureIdentity) -> PlaybackResult:
    """Fetch and validate one exact capture identity."""

    expected_status = (
        int(identity.status_token) if identity.status_token.isdigit() else None
    )
    memento = client.get_memento(
        identity.original_url,
        timestamp=identity.timestamp,
        mode=Mode.original,
        exact=True,
        follow_redirects=False,
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
    if not same_original_url(url, identity.original_url):
        raise ExactMismatchError(
            f"URL mismatch: requested {identity.original_url}, got {url}"
        )
    if expected_status is not None and status_code != expected_status:
        raise ExactMismatchError(
            f"status mismatch: requested {expected_status}, got {status_code}"
        )
    return PlaybackResult(
        identity=identity,
        body=body,
        status_code=status_code,
        headers=headers,
        warc_date=returned_ts,
        source_uri=memento_url,
        warc_payload_digest=payload_digest(body),
        digest_matched=cdx_digest_matches_body(identity.payload_digest, body),
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
    if status_code != 206:
        skip.add("content-range")
    semantic = [
        (name, value)
        for name, value in headers.items()
        if name.lower() not in skip
    ]
    semantic.append(("Content-Length", str(payload_length)))
    return semantic
