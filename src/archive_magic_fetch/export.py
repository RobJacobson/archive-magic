"""Per-CDX-key capture export policy."""

from __future__ import annotations

import base64
import binascii
import sys
from pathlib import Path
from typing import Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

from .retrieval import (
    CaptureRetrievalError,
    PlaybackSubstitution,
    SourceDigestMismatch,
    retrieve_response,
)
from .warc import open_new_warc, prepare_response, write_response, write_revisit


def normalize_digest(value: object) -> Optional[str]:
    """Normalize a usable SHA-1 Base32 digest to warcio's representation."""

    if not isinstance(value, str):
        return None

    digest = value.strip()
    if not digest or digest == "-":
        return None

    if ":" in digest:
        algorithm, digest = digest.split(":", 1)
        if algorithm.lower() != "sha1":
            return None

    encoded = digest.upper()
    if len(encoded) != 32:
        return None

    try:
        decoded = base64.b32decode(encoded, casefold=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 20:
        return None

    return f"sha1:{encoded}"


def _capture_status(capture: MutableMapping) -> Optional[str]:
    """Return a usable CDX status, or None for a source revisit/unknown."""

    status = capture.get("status")
    if status is None:
        return None
    value = str(status).strip()
    if not value or value == "-":
        return None
    return value


def _warn_skip(capture: MutableMapping, reason: str) -> None:
    print(
        f"WARNING skipped {capture['timestamp']} {capture['url']}: {reason}",
        file=sys.stderr,
    )


def _canonical_alias_identity(url: str) -> Optional[tuple]:
    """Return the conservative identity used for canonical redirect omission."""

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None

    host = parsed.hostname.lower()
    if host.startswith("www."):
        host = host[4:]

    try:
        port = parsed.port
    except ValueError:
        return None
    if (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    ):
        port = None

    return host, port, parsed.path or "/", parsed.query


def _is_canonical_alias_redirect(source_url: str, location: str) -> bool:
    """Return whether a redirect changes only scheme, www, or default port."""

    target_url = urljoin(source_url, location)
    source_identity = _canonical_alias_identity(source_url)
    return (
        source_identity is not None
        and source_identity == _canonical_alias_identity(target_url)
    )


def _is_redirect_status(status: Optional[str]) -> bool:
    return bool(status and status.isdigit() and 300 <= int(status) <= 399)


def export_group(
    urlkey: str, captures: Sequence[MutableMapping], path: Path
) -> None:
    """Export one CDX URL-key group with shared payload deduplication."""

    if not captures:
        raise ValueError(f"capture group is empty: {urlkey}")

    source_by_signature = {}
    source_by_digest = {}
    content_by_signature = {}
    content_by_digest = {}
    omitted_source_signatures = set()
    omitted_alias_redirects = 0
    stream = None
    writer = None

    representative_url = captures[0]["url"]
    variants = len({capture["url"] for capture in captures})
    suffix = f" ({variants} URL variants)" if variants != 1 else ""
    print(f"Starting {representative_url}{suffix}")

    try:
        for capture in captures:
            target_url = capture["url"]
            expected = normalize_digest(capture.get("digest"))
            status = _capture_status(capture)
            source_signature = (
                (expected, status)
                if expected is not None and status is not None
                else None
            )

            if source_signature in omitted_source_signatures:
                omitted_alias_redirects += 1
                continue

            source_match = None
            if expected is not None:
                if status is None:
                    source_match = source_by_digest.get(expected)
                else:
                    source_match = source_by_signature.get(
                        (expected, status)
                    )

            if source_match is not None:
                if writer is None:  # pragma: no cover - map/writer invariant
                    raise RuntimeError("canonical response exists without an open WARC")
                normalized_digest, canonical = source_match
                write_revisit(
                    writer,
                    target_url,
                    capture["timestamp"],
                    normalized_digest,
                    canonical,
                )
                continue

            try:
                retrieved = retrieve_response(capture, expected)
                response = retrieved.record
            except PlaybackSubstitution as error:
                if (
                    error.target_url
                    and _is_canonical_alias_redirect(
                        target_url, error.target_url
                    )
                ):
                    omitted_alias_redirects += 1
                    if source_signature is not None:
                        omitted_source_signatures.add(source_signature)
                    continue
                _warn_skip(capture, str(error))
                continue
            except SourceDigestMismatch:
                _warn_skip(capture, "payload digest mismatch")
                continue
            except CaptureRetrievalError as error:
                _warn_skip(capture, str(error))
                continue
            except Exception as error:
                detail = str(error) or type(error).__name__
                _warn_skip(
                    capture,
                    f"capture unavailable ({type(error).__name__}: {detail})",
                )
                continue

            prepare_response(response, target_url, capture["timestamp"])
            actual = normalize_digest(
                response.rec_headers.get_header("WARC-Payload-Digest")
            )
            if actual is None:
                _warn_skip(capture, "calculated payload digest unavailable")
                continue

            if (
                not retrieved.source_verified
                and expected is not None
                and actual != expected
            ):
                _warn_skip(capture, "payload digest mismatch")
                continue

            response_status = status
            if response_status is None and response.http_headers is not None:
                response_status = response.http_headers.get_statuscode()

            location = (
                response.http_headers.get_header("Location")
                if response.http_headers is not None
                else None
            )
            if (
                _is_redirect_status(response_status)
                and location
                and _is_canonical_alias_redirect(target_url, location)
            ):
                omitted_alias_redirects += 1
                if source_signature is not None:
                    omitted_source_signatures.add(source_signature)
                continue

            if writer is None:
                stream, writer = open_new_warc(path)

            content_canonical = None
            if response_status is None:
                content_canonical = content_by_digest.get(actual)
            else:
                content_canonical = content_by_signature.get(
                    (actual, str(response_status))
                )

            if content_canonical is None:
                canonical = write_response(writer, response)
            else:
                canonical = content_canonical
                write_revisit(
                    writer,
                    target_url,
                    capture["timestamp"],
                    actual,
                    canonical,
                )
            print(f"Downloaded {capture['timestamp']} [{actual[-8:]}]")

            content_by_digest.setdefault(actual, canonical)
            if response_status is not None:
                content_by_signature.setdefault(
                    (actual, str(response_status)), canonical
                )

            if expected is not None:
                source_match = (actual, canonical)
                source_by_digest.setdefault(expected, source_match)
                if status is not None:
                    source_by_signature.setdefault(
                        (expected, status), source_match
                    )
    finally:
        if stream is not None:
            stream.close()

    if omitted_alias_redirects:
        noun = "redirect" if omitted_alias_redirects == 1 else "redirects"
        print(f"Omitted {omitted_alias_redirects} canonical URL {noun}")


def export_all(
    capture_groups: Mapping[str, Sequence[MutableMapping]],
    output_paths: Mapping[str, Path],
) -> None:
    """Export each CDX URL-key group in discovery order."""

    for urlkey, captures in capture_groups.items():
        export_group(urlkey, captures, output_paths[urlkey])
