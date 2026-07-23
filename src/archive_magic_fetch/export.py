"""Per-URL capture export policy."""

from __future__ import annotations

import base64
import binascii
import sys
from pathlib import Path
from typing import Mapping, MutableMapping, Optional, Sequence

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


def is_redirect(capture: MutableMapping) -> bool:
    """Return whether the authoritative CDX status is in the 3xx range."""

    try:
        status = int(capture.get("status", ""))
    except (TypeError, ValueError):
        return False
    return 300 <= status <= 399


def _warn_skip(capture: MutableMapping, reason: str) -> None:
    print(
        f"WARNING skipped {capture['timestamp']} {capture['url']}: {reason}",
        file=sys.stderr,
    )


def export_url(url: str, captures: Sequence[MutableMapping], path: Path) -> None:
    """Export all successful captures for one exact URL."""

    seen = {}
    stream = None
    writer = None

    print(f"Starting {url}")

    try:
        for capture in captures:
            expected = normalize_digest(capture.get("digest"))
            redirect = is_redirect(capture)

            if not redirect and expected is not None and expected in seen:
                if writer is None:  # pragma: no cover - map/writer invariant
                    raise RuntimeError("canonical response exists without an open WARC")
                write_revisit(
                    writer,
                    url,
                    capture["timestamp"],
                    expected,
                    seen[expected],
                )
                continue

            try:
                response = capture.fetch_warc_record()
            except Exception:
                _warn_skip(capture, "capture unavailable")
                continue

            prepare_response(response, url, capture["timestamp"])
            actual = normalize_digest(
                response.rec_headers.get_header("WARC-Payload-Digest")
            )
            if actual is None:
                _warn_skip(capture, "calculated payload digest unavailable")
                continue

            if expected is not None and actual != expected:
                _warn_skip(capture, "payload digest mismatch")
                continue

            if writer is None:
                stream, writer = open_new_warc(path)

            canonical = write_response(writer, response)
            print(f"Downloaded {capture['timestamp']} [{actual[-8:]}]")

            if not redirect:
                seen.setdefault(actual, canonical)
    finally:
        if stream is not None:
            stream.close()


def export_all(
    captures_by_url: Mapping[str, Sequence[MutableMapping]],
    output_paths: Mapping[str, Path],
) -> None:
    """Export each exact URL independently in discovery group order."""

    for url, captures in captures_by_url.items():
        export_url(url, captures, output_paths[url])

