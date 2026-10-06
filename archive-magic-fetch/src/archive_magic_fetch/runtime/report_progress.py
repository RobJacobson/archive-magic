"""Terminal progress and mirrored run logs."""

from __future__ import annotations

import os
import re
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from archive_magic_fetch.models import (
    CaptureIdentity,
    CaptureKind,
    CaptureOutcome,
    UrlOutcome,
)

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


_LOG_STREAM: TextIO | None = None


_ESCAPE = re.compile(r"\x1b(?:\][^\x1b]*(?:\x1b\\|\x07)|\[[0-?]*[ -/]*[@-~])")


@contextmanager
def mirror_output(path: Path) -> Iterator[None]:
    """Mirror emitted console output to a plain-text log."""

    global _LOG_STREAM
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", buffering=1) as stream:
        with _LOCK:
            _LOG_STREAM = stream
        try:
            yield
        finally:
            with _LOCK:
                _LOG_STREAM = None


def emit(text: str) -> None:
    """Print one line; safe to call from worker threads."""

    with _LOCK:
        print(text, flush=True)
        if _LOG_STREAM is not None:
            _LOG_STREAM.write(_ESCAPE.sub("", text) + "\n")


def links_enabled() -> bool:
    stdout = getattr(sys.stdout, "isatty", lambda: False)
    return bool(stdout() and os.environ.get("TERM", "") != "dumb")


def color_enabled() -> bool:
    return links_enabled() and "NO_COLOR" not in os.environ


def timestamp_link(
    identity: CaptureIdentity,
    *,
    destination: str | None = None,
    enabled: bool | None = None,
) -> str:
    """Render a capture timestamp, linked to its source when the terminal allows it."""

    ts = identity.timestamp
    label = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}"
    if destination is None or not (links_enabled() if enabled is None else enabled):
        return label
    destination = _safe(destination)
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


def log_url_outcome(
    number: int,
    total: int,
    outcome: UrlOutcome,
    *,
    capture_link=lambda ref: ref.identity.original_url,
) -> None:
    links = links_enabled()
    color = color_enabled()
    lines = [f"{number}/{total} {_safe(outcome.url)}"]
    for capture in outcome.captures:
        # Skip already-represented captures: backfills otherwise drown the
        # console in dim noise while URL progress lines remain useful.
        if capture.kind is CaptureKind.EXISTING:
            continue
        detail, style = _capture_line(capture)
        lines.append(
            f"  {timestamp_link(capture.capture.identity, destination=capture_link(capture.capture), enabled=links)}  "
            f"{_safe(capture.capture.identity.payload_digest[-6:]):>6}  "
            f"{style_result(detail, style, enabled=color)}"
        )
    emit("\n".join(lines))


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
    if outcome.kind is CaptureKind.FAILURE:
        assert outcome.failure is not None
        reason = outcome.failure.category.value.replace("_", " ")
        reason = outcome.failure.display_reason or reason
        return _with_timing(f"Ignored [{reason}]", outcome), "warning"

    assert outcome.kind is CaptureKind.DOWNLOADED
    assert outcome.playback is not None
    extra = playback_timing(outcome)
    if outcome.playback.source_repairs:
        extra += ", recovered: " + ", ".join(outcome.playback.source_repairs)
    if not outcome.playback.digest_matched:
        extra += ", digest mismatch kept"
    style = "warning" if not outcome.playback.digest_matched else "success"
    return f"Downloaded ({extra})", style
