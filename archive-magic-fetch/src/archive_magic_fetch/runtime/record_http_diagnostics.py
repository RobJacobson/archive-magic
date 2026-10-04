"""Bounded, best-effort diagnostics for rejected HTTP responses."""

from __future__ import annotations

import json
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

BODY_EXCERPT_BYTES = 4096
BODY_WIRE_BYTES = 8192
HEADER_VALUE_CHARS = 1024
DIAGNOSTIC_HEADERS = (
    "Date", "Server", "Content-Type", "Content-Length", "Content-Encoding",
    "Retry-After", "Via", "Age", "X-Cache", "X-Cache-Hits", "X-Request-Id",
    "CF-Ray", "CF-Mitigated", "Memento-Datetime", "X-Archive-Src",
    "X-Archive-Wayback-Runtime-Error", "X-Archive-Orig-Status",
    "X-Archive-Orig-Retry-After",
)


class RateLimitDiagnostics:
    """Write one JSON line per 429, without logging cookies or request headers."""

    def __init__(self, path: Path, report: Callable[[str], None]) -> None:
        self.path = path
        self._report = report
        self._lock = threading.Lock()
        self._stream: TextIO | None = None
        self._closed = False

    def record(self, request_id: int, phase: str, response) -> None:
        record = {
            "request_id": request_id,
            "phase": phase,
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "url": response.url,
            "status": response.status_code,
            "headers": {
                name: response.headers[name][:HEADER_VALUE_CHARS]
                for name in DIAGNOSTIC_HEADERS if name in response.headers
            },
            **_body_excerpt(response),
        }
        # Body I/O must not hold a lock needed by other requests or shutdown.
        try:
            with self._lock:
                if self._closed:
                    return
                if self._stream is None:
                    self._stream = self.path.open("x", encoding="utf-8", buffering=1)
                self._stream.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError as error:
            self._report(f"could not save HTTP 429 diagnostics: {error}")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stream is not None:
                self._stream.close()
                self._stream = None


def _body_excerpt(response) -> dict[str, object]:
    """Read a bounded rejected body, then close it so retries do not drain it."""

    body = b""
    details: dict[str, object] = {}
    try:
        if response._content is not False:
            body = response._content or b""
        else:
            body = response.raw.read(BODY_WIRE_BYTES + 1, decode_content=False)
            limited = len(body) > BODY_WIRE_BYTES
            body = body[:BODY_WIRE_BYTES]
            encoding = response.headers.get("Content-Encoding", "").lower().strip()
            if encoding in ("gzip", "x-gzip") and body.startswith(b"\x1f\x8b"):
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                body = decoder.decompress(body, BODY_EXCERPT_BYTES + 1)
                limited = limited or not decoder.eof
            elif encoding == "deflate":
                decoder = zlib.decompressobj()
                body = decoder.decompress(body, BODY_EXCERPT_BYTES + 1)
                limited = limited or not decoder.eof
            elif encoding and encoding not in ("identity", "gzip", "x-gzip"):
                details["body_read_error"] = f"unsupported Content-Encoding: {encoding}"
                body = b""
            details["body_truncated"] = limited
        details["body_truncated"] = bool(
            details.get("body_truncated") or len(body) > BODY_EXCERPT_BYTES
        )
        body = body[:BODY_EXCERPT_BYTES]
        details["body_excerpt"] = body.decode(response.encoding or "utf-8", errors="replace")
    except Exception as error:
        details["body_excerpt"] = ""
        details["body_read_error"] = f"{type(error).__name__}: {error}"[:1024]
    finally:
        # Only 429s reach this function. They are rejected by both source
        # adapters; never materialize the remainder just to discard it.
        try:
            response.close()
        except Exception as error:
            details["body_close_error"] = f"{type(error).__name__}: {error}"[:1024]
        response._content = body[:BODY_EXCERPT_BYTES]
        response._content_consumed = True
    return details
