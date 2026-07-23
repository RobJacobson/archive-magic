"""Verified Internet Archive playback retrieval with content normalization."""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Callable, Mapping, Optional
from urllib.parse import quote, urlparse

import requests
from cdx_toolkit.myrequests import get_retries, update_next_fetch
from warcio.recordbuilder import RecordBuilder
from warcio.statusandheaders import StatusAndHeaders


class CaptureRetrievalError(RuntimeError):
    """A remote capture could not be reconstructed safely."""


class SourceDigestMismatch(CaptureRetrievalError):
    """Wayback returned no representation matching the selected CDX digest."""


class PlaybackSubstitution(CaptureRetrievalError):
    """Wayback substituted a replay response for the selected CDX capture."""

    def __init__(self, message: str, target_url: Optional[str] = None):
        super().__init__(message)
        self.target_url = target_url


@dataclass(frozen=True)
class RetrievedResponse:
    """A normalized response and whether its CDX source digest was verified."""

    record: object
    source_verified: bool


def _sha1_digest(payload: bytes) -> str:
    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def _decode_deflate(payload: bytes) -> bytes:
    try:
        return zlib.decompress(payload)
    except zlib.error:
        return zlib.decompress(payload, -zlib.MAX_WBITS)


def _decode_content(payload: bytes, content_encoding: Optional[str]) -> bytes:
    """Decode an HTTP Content-Encoding chain into semantic content bytes."""

    if not content_encoding:
        return payload

    encodings = [
        encoding.strip().lower()
        for encoding in content_encoding.split(",")
        if encoding.strip()
    ]
    decoded = payload
    for encoding in reversed(encodings):
        try:
            if encoding in {"identity"}:
                continue
            if encoding in {"gzip", "x-gzip"}:
                decoded = gzip.decompress(decoded)
                continue
            if encoding == "deflate":
                decoded = _decode_deflate(decoded)
                continue
        except (OSError, EOFError, zlib.error) as error:
            raise CaptureRetrievalError(
                f"cannot decode {encoding} content"
            ) from error
        raise CaptureRetrievalError(
            f"unsupported content encoding: {encoding}"
        )
    return decoded


def _stream_get(url: str, *, expected_status: Optional[int] = None):
    """Fetch one raw playback response with IA pacing and bounded retries."""

    hostname = urlparse(url).hostname or ""
    next_fetch, minimum_interval = get_retries(hostname)
    now = time.time()
    if now < next_fetch:
        time.sleep(next_fetch - now)

    retry_delay = max(2 * minimum_interval, 1.0)
    response = None
    for attempt in range(4):
        try:
            response = requests.get(
                url,
                headers={
                    "User-Agent": "archive-magic-fetch/0.1.0",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=(30.0, 30.0),
                allow_redirects=False,
                stream=True,
            )
            if (
                response.status_code == expected_status
                or response.status_code
                not in {429, 500, 502, 503, 504, 509}
            ):
                break
            response.close()
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.Timeout,
        ) as error:
            if attempt == 3:
                raise CaptureRetrievalError(str(error)) from error

        if attempt == 3:
            break
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 2, 60.0)

    update_next_fetch(hostname, time.time() + minimum_interval)
    if response is None:
        raise CaptureRetrievalError("playback returned no response")
    if (
        response.status_code != expected_status
        and response.status_code in {429, 500, 502, 503, 504, 509}
    ):
        raise CaptureRetrievalError(
            f"playback failed with HTTP {response.status_code}"
        )
    return response


def _wayback_location_to_original(location: str) -> str:
    marker = "_/http"
    if marker not in location:
        return location
    return "http" + location.split(marker, 1)[1]


def _link_original_url(value: Optional[str]) -> Optional[str]:
    """Extract the original URI from a Wayback Memento Link header."""

    if not value:
        return None

    for match in re.finditer(r"<([^>]*)>\s*;([^,]*)", value):
        parameters = match.group(2)
        if re.search(
            r'\brel\s*=\s*(?:"original"|original)(?:\s*;|\s*$)',
            parameters,
            re.I,
        ):
            return match.group(1)
    return None


def _playback_substitution(capture, response) -> Optional[PlaybackSubstitution]:
    """Identify Wayback routing or fallback responses, not origin responses."""

    reason = response.headers.get("X-Archive-Redirect-Reason")
    if reason and 300 <= response.status_code <= 399:
        location = response.headers.get("Location")
        target_url = (
            _wayback_location_to_original(location) if location else None
        )
        detail = f"Wayback substituted HTTP {response.status_code}: {reason}"
        if target_url:
            detail += f" ({target_url})"
        return PlaybackSubstitution(detail, target_url)

    indexed_status_text = str(capture.get("status", "")).strip()
    if (
        indexed_status_text.isdigit()
        and 300 <= int(indexed_status_text) <= 399
        and not 300 <= response.status_code <= 399
    ):
        target_url = _link_original_url(response.headers.get("Link"))
        if target_url and target_url != capture["url"]:
            return PlaybackSubstitution(
                f"Wayback substituted HTTP {response.status_code} "
                f"capture ({target_url})",
                target_url,
            )

    return None


def _original_headers(
    headers: Mapping[str, str],
    *,
    direct_content_encoding_is_original: bool,
) -> list[tuple[str, str]]:
    """Extract the archived HTTP headers from Wayback playback headers."""

    original: list[tuple[str, str]] = []
    original_names = set()

    for name, value in headers.items():
        lowered = name.lower()
        if lowered.startswith("x-archive-orig-"):
            restored_name = name[len("x-archive-orig-") :]
            original.append((restored_name, value))
            original_names.add(restored_name.lower())

    for name, value in headers.items():
        lowered = name.lower()
        if lowered == "content-type" and lowered not in original_names:
            original.append(("Content-Type", value))
        elif lowered == "location" and lowered not in original_names:
            original.append(("Location", _wayback_location_to_original(value)))
        elif (
            lowered == "content-encoding"
            and direct_content_encoding_is_original
            and lowered not in original_names
        ):
            original.append(("Content-Encoding", value))

    return original


def _header_value(headers: list[tuple[str, str]], name: str) -> Optional[str]:
    lowered_name = name.lower()
    for header_name, value in headers:
        if header_name.lower() == lowered_name:
            return value
    return None


def _normalized_headers(
    headers: list[tuple[str, str]],
    payload_length: int,
    *,
    content_transformed: bool,
) -> list[tuple[str, str]]:
    """Repair headers so they describe the decoded payload written to WARC."""

    always_remove = {"content-encoding", "content-length", "transfer-encoding"}
    transformed_remove = {
        "content-md5",
        "content-digest",
        "content-range",
        "digest",
        "etag",
        "repr-digest",
    }

    normalized = []
    for name, value in headers:
        lowered = name.lower()
        if lowered in always_remove:
            continue
        if content_transformed and lowered in transformed_remove:
            continue
        normalized.append((name, value))
    normalized.append(("Content-Length", str(payload_length)))
    return normalized


def _response_status(capture, response) -> tuple[int, str]:
    status_code = response.status_code
    status_reason = response.reason or ""
    indexed_status = str(capture.get("status", ""))

    if indexed_status.isdigit() and 300 <= int(indexed_status) <= 399:
        if status_code == 302:
            status_code = int(indexed_status)
            reasons = {
                300: "Multiple Choices",
                301: "Moved Permanently",
                302: "Found",
                303: "See Other",
                304: "Not Modified",
                307: "Temporary Redirect",
                308: "Permanent Redirect",
            }
            status_reason = reasons.get(status_code, status_reason)

    return status_code, status_reason


def fetch_normalized_ia_response(
    capture,
    expected_source_digest: Optional[str],
    *,
    http_get: Optional[Callable[[str], object]] = None,
) -> RetrievedResponse:
    """Fetch, source-verify, decode, and reconstruct one IA capture."""

    url = capture["url"]
    timestamp = capture["timestamp"]
    indexed_status_text = str(capture.get("status", "")).strip()
    indexed_status = (
        int(indexed_status_text)
        if indexed_status_text.isdigit()
        else None
    )
    playback_base = getattr(capture, "wb", None) or "https://web.archive.org/web"
    playback_url = f"{playback_base}/{timestamp}id_/{quote(url)}"
    if http_get is None:
        response = _stream_get(
            playback_url,
            expected_status=indexed_status,
        )
    else:
        response = http_get(playback_url)

    substitution = _playback_substitution(capture, response)
    if substitution is not None:
        response.close()
        raise substitution

    status_matches_index = (
        indexed_status is not None
        and response.status_code == indexed_status
    )
    allow_source_revisit_404 = (
        indexed_status_text == "-" and response.status_code == 404
    )
    if (
        response.status_code >= 400
        and not status_matches_index
        and not allow_source_revisit_404
    ):
        response.close()
        displayed_status = indexed_status_text or "unknown"
        raise CaptureRetrievalError(
            f"playback returned HTTP {response.status_code} "
            f"for indexed status {displayed_status}"
        )

    try:
        raw_payload = response.raw.read(decode_content=False)
    except Exception as error:
        raise CaptureRetrievalError("cannot read playback payload") from error
    finally:
        response.close()

    direct_encoding = response.headers.get("Content-Encoding")
    playback_decoded = _decode_content(raw_payload, direct_encoding)
    raw_digest = _sha1_digest(raw_payload)
    decoded_digest = _sha1_digest(playback_decoded)

    source_verified = expected_source_digest is not None
    if expected_source_digest is None:
        source_payload = raw_payload
        direct_encoding_is_original = bool(direct_encoding)
        decode_archived_header_after_playback = True
        normalized_payload = playback_decoded
    elif raw_digest == expected_source_digest:
        source_payload = raw_payload
        direct_encoding_is_original = bool(direct_encoding)
        decode_archived_header_after_playback = not bool(direct_encoding)
        normalized_payload = playback_decoded
    elif decoded_digest == expected_source_digest:
        source_payload = playback_decoded
        direct_encoding_is_original = False
        decode_archived_header_after_playback = True
        normalized_payload = playback_decoded
    else:
        raise SourceDigestMismatch(
            f"expected {expected_source_digest}, raw {raw_digest}, "
            f"decoded {decoded_digest}"
        )

    archived_headers = _original_headers(
        response.headers,
        direct_content_encoding_is_original=direct_encoding_is_original,
    )
    archived_encoding = _header_value(archived_headers, "Content-Encoding")

    if (
        archived_encoding
        and decode_archived_header_after_playback
    ):
        try:
            normalized_payload = _decode_content(
                normalized_payload, archived_encoding
            )
        except CaptureRetrievalError:
            if not (
                expected_source_digest is None
                and archived_encoding.lower()
                == (direct_encoding or "").lower()
            ):
                raise

    content_transformed = normalized_payload != source_payload
    headers = _normalized_headers(
        archived_headers,
        len(normalized_payload),
        content_transformed=content_transformed,
    )
    status_code, status_reason = _response_status(capture, response)
    http_headers = StatusAndHeaders(
        f"{status_code} {status_reason}".rstrip(),
        headers,
        protocol="HTTP/1.1",
    )

    warc_headers = {
        "WARC-Source-URI": playback_url,
        "WARC-Creation-Date": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    builder = RecordBuilder(warc_version="1.0")
    record = builder.create_warc_record(
        url,
        "response",
        payload=BytesIO(normalized_payload),
        length=len(normalized_payload),
        http_headers=http_headers,
        warc_headers_dict=warc_headers,
    )
    return RetrievedResponse(record=record, source_verified=source_verified)


def retrieve_response(
    capture, expected_source_digest: Optional[str]
) -> RetrievedResponse:
    """Normalize real IA captures while retaining simple fake-capture tests."""

    if getattr(capture, "wb", None):
        return fetch_normalized_ia_response(capture, expected_source_digest)
    return RetrievedResponse(
        record=capture.fetch_warc_record(),
        source_verified=False,
    )
