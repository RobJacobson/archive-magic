"""Terminal presentation for fetch progress."""

from __future__ import annotations

import os
import sys
import threading
from typing import Sequence

from .identity import is_invalid_uri_payload_digest, wayback_url
from .models import CaptureIdentity, FailureCategory, UnresolvedFailure
from .resolution import CaptureKind, CaptureOutcome, UrlOutcome

_RESULT_STYLES = {
    "success": "32",
    "revisit": "36",
    "warning": "33",
    "error": "1;31",
    "dim": "2",
}
_OSC = "\033]8;;"
_ST = "\033\\"
_LOCK = threading.Lock()


def emit(text: str) -> None:
    """Print one line; safe to call from worker threads."""

    with _LOCK:
        print(text, flush=True)


def links_enabled() -> bool:
    stdout = getattr(sys.stdout, "isatty", lambda: False)
    return bool(stdout() and os.environ.get("TERM", "") != "dumb")


def color_enabled() -> bool:
    return links_enabled() and "NO_COLOR" not in os.environ


def timestamp_link(identity: CaptureIdentity, *, enabled: bool | None = None) -> str:
    """Render a capture timestamp, linked to Wayback when the terminal allows it."""

    ts = identity.timestamp
    label = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    if not (links_enabled() if enabled is None else enabled):
        return label
    destination = _safe(wayback_url(identity.timestamp, identity.original_url))
    return f"{_OSC}{destination}{_ST}{label}{_OSC}{_ST}"


def style_result(text: str, style: str, *, enabled: bool | None = None) -> str:
    """Apply an ANSI result style when color output is appropriate."""

    if not (color_enabled() if enabled is None else enabled):
        return text
    return f"\033[{_RESULT_STYLES[style]}m{text}\033[0m"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def playback_timing(outcome: CaptureOutcome) -> str:
    text = f"{outcome.elapsed_s:.1f}s"
    if outcome.attempts > 1:
        text += f", {outcome.attempts} attempts"
    return text


def log_url_outcome(number: int, total: int, outcome: UrlOutcome) -> None:
    links = links_enabled()
    color = color_enabled()
    lines = [
        f"{number}/{total} {_safe(outcome.url)}",
        "  Capture              Digest  Result",
    ]
    for capture in outcome.captures:
        detail, style = _capture_line(capture)
        lines.append(
            f"  {timestamp_link(capture.identity, enabled=links)}  "
            f"{_safe(capture.identity.payload_digest[-6:]):>6}  "
            f"{style_result(detail, style, enabled=color)}"
        )
    emit("\n".join(lines))


def report_cdx_ingest_skips(year: int, failures: Sequence[UnresolvedFailure]) -> None:
    malformed = [
        item for item in failures if item.category == FailureCategory.MALFORMED_CDX
    ]
    if not malformed:
        return
    emit(f"year {year}: skipping {len(malformed)} malformed CDX row(s)")
    preview = malformed[:5]
    for item in preview:
        url = item.identity.original_url
        if url and url != "-":
            emit(f"  skip: {url}")
            emit(f"        {item.message}")
        else:
            emit(f"  skip: {item.message}")
    extra = len(malformed) - len(preview)
    if extra:
        emit(f"  ... and {extra} more")


def _safe(value: str) -> str:
    return "".join(char if char.isprintable() else "?" for char in value)


def _with_timing(label: str, outcome: CaptureOutcome) -> str:
    if not outcome.attempts:
        return label
    return f"{label} ({playback_timing(outcome)})"


def _capture_line(outcome: CaptureOutcome) -> tuple[str, str]:
    if outcome.kind is CaptureKind.EXISTING:
        return "Ignored [already represented]", "dim"
    if outcome.kind is CaptureKind.REVISIT:
        return "Revisit", "revisit"
    if outcome.kind is CaptureKind.EMPTY:
        return "Empty payload", "revisit"
    if outcome.kind is CaptureKind.FAILURE:
        assert outcome.failure is not None
        reason = outcome.failure.category.value.replace("_", " ")
        if is_invalid_uri_payload_digest(outcome.identity.payload_digest):
            reason = "invalid URI"
        return _with_timing(f"Ignored [{reason}]", outcome), "warning"
    if outcome.kind is CaptureKind.SLASH_REDIRECT:
        return _with_timing("Slash redirect", outcome), "revisit"

    assert outcome.kind is CaptureKind.DOWNLOADED
    assert outcome.playback is not None
    extra = playback_timing(outcome)
    if outcome.playback.substituted:
        extra += ", substituted"
    if not outcome.playback.digest_matched:
        extra += ", digest mismatch kept"
    style = (
        "warning"
        if outcome.playback.substituted or not outcome.playback.digest_matched
        else "success"
    )
    return f"Downloaded ({extra})", style
