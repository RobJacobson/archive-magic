"""Classify Common Crawl failures without imposing Wayback replay policies."""

import errno

import requests

from archive_magic_fetch.contracts import FailureAdvice
from archive_magic_fetch.models import FailureCategory
from archive_magic_fetch.runtime.calculate_retry_delay import (
    iter_error_chain,
    retry_after_from_error,
)


def failure_advice(error: BaseException, attempt: int) -> FailureAdvice:
    delay = max(min(5 * 2 ** (attempt - 1), 60), retry_after_from_error(error) or 0)
    if isinstance(error, UnsupportedRecord):
        return FailureAdvice(FailureCategory.UNAVAILABLE, False)
    if isinstance(error, SourceTruncated):
        return FailureAdvice(FailureCategory.TRUNCATED, False)
    if isinstance(error, IdentityMismatch):
        return FailureAdvice(FailureCategory.EXACT_MISMATCH, False)
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    refused = any(
        isinstance(item, ConnectionRefusedError)
        or getattr(item, "errno", None) == errno.ECONNREFUSED
        for item in iter_error_chain(error)
    )
    if status in (429, 503) or refused:
        label = f"HTTP {status}" if status in (429, 503) else "TCP connection refused"
        return FailureAdvice(
            FailureCategory.RETRY_EXHAUSTED, True,
            cooldown=("http" if status else "tcp", max(60, delay), label),
        )
    retryable = (
        isinstance(error, (CorruptRecord, requests.Timeout, requests.ConnectionError,
                           requests.exceptions.ChunkedEncodingError))
        or (status is not None and 500 <= status < 600)
    )
    return FailureAdvice(
        FailureCategory.RETRY_EXHAUSTED if retryable else FailureCategory.UNAVAILABLE,
        retryable, delay=delay,
    )


class CorruptRecord(ValueError):
    """An incomplete or invalid range/record may be retried as one acquisition."""


class UnsupportedRecord(ValueError):
    """A source format requires capabilities deliberately outside this adapter."""


class SourceTruncated(ValueError):
    """The crawler explicitly recorded an incomplete original response."""


class IdentityMismatch(ValueError):
    """The record does not match the selected index identity."""
