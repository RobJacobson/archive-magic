"""Typed source operations consumed by the shared pipeline."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from archive_magic_fetch.models import (
    CaptureListing,
    CaptureRef,
    CaptureResult,
    FailureCategory,
    UnresolvedFailure,
)
from archive_magic_fetch.runtime.track_http_requests import RequestStats

Client = TypeVar("Client")


@dataclass(frozen=True)
class DiscoveryRequest:
    """One output year's complete acquisition scope, with a run-stable UTC year."""

    url_pattern: str
    year: int
    current_year: int
    on_cache_complete: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class FailureAdvice:
    """Source interpretation of one failed acquisition attempt.

    A cooldown contains its kind, minimum delay, and display label. Failure
    groups limit repeated failures of the same kind within one capture; they
    do not replace the run's total attempt limit.
    """

    category: FailureCategory
    retryable: bool
    delay: float = 0.0
    cooldown: tuple[str, float | None, str] | None = None
    group: str | None = None
    group_limit: int | None = None
    display_reason: str | None = None


@dataclass(frozen=True)
class SourceAdapter(Generic[Client]):
    """Bound source operations; fetch includes decoding but never owns retries."""

    discover: Callable[[DiscoveryRequest, RequestStats | None], CaptureListing]
    open_client: Callable[[RequestStats], AbstractContextManager[Client]]
    fetch: Callable[[Client, CaptureRef], CaptureResult]
    preflight: Callable[[CaptureRef], UnresolvedFailure | None]
    failure_advice: Callable[[BaseException, int], FailureAdvice]
    capture_link: Callable[[CaptureRef], str]
    name: str = "wayback"
