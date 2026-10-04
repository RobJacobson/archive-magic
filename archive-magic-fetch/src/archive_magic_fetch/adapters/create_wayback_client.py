"""Construct Wayback clients with replay-specific transport repair."""

from __future__ import annotations

import gzip
from urllib.parse import urlsplit

import requests
from wayback import WaybackClient, WaybackSession
from wayback._client import read_and_close
from wayback.exceptions import RateLimitError

from archive_magic_fetch.runtime.track_http_requests import InstrumentedHTTPAdapter, RequestStats
from archive_magic_fetch.runtime.calculate_retry_delay import parse_retry_after

_GZIP_MAGIC = b"\x1f\x8b"


def make_client() -> WaybackClient:
    """Return a playback client paced by Archive Magic's shared gate."""

    return WaybackClient(
        session=ArchiveMagicWaybackSession(
            user_agent="archive-magic-fetch",
            memento_calls_per_second=0,
        )
    )


class ArchiveMagicWaybackSession(WaybackSession):
    """Wayback session tuned for Archive Magic fetch.

    Playback clients leave library retries disabled because Fetch owns their
    synchronous retry loop. CDX clients also leave library retries disabled;
    `_fetch_cdx` owns CDX retries: transient 504s, rate limits, and wall-clock
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

    def track_playback(self, stats: RequestStats) -> None:
        """Apply one shared gate and counter at the HTTP transport boundary."""

        self.retries = 0
        for prefix in ("https://", "http://"):
            self.adapters[prefix].close()
            self.mount(prefix, InstrumentedHTTPAdapter(stats))

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
        try:
            _repair_false_gzip_content_encoding(response)
        except BaseException as error:
            try:
                response.close()
            except Exception as cleanup_error:
                error.add_note(f"Response cleanup also failed: {cleanup_error}")
            raise
        return response


def _repair_false_gzip_content_encoding(response: requests.Response) -> None:
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
