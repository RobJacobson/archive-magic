"""Focused semantic capture-resolution tests."""

from dataclasses import replace
from unittest.mock import MagicMock

from archive_magic_fetch.identity import revisit_group_key
from archive_magic_fetch.inventory import stored_from_playback
from archive_magic_fetch.models import FailureCategory, ParsedCapture, UnresolvedFailure
from archive_magic_fetch.protocol import EMPTY_PAYLOAD_DIGEST
from archive_magic_fetch.resolution import CaptureKind, _resolve_capture
from archive_magic_fetch.workers import DownloadOutcome
from helpers import make_capt, playback


def resolve(
    capture: ParsedCapture,
    *,
    downloaded: DownloadOutcome | None = None,
    existing=frozenset(),
    representatives=None,
    group_urls=None,
):
    workers = MagicMock()
    if downloaded is not None:
        workers.download.return_value = downloaded
    outcome, download = _resolve_capture(
        capture,
        group_urls=group_urls or (capture.identity.original_url,),
        workers=workers,
        existing_identities=existing,
        existing_representatives=representatives or {},
        local_representatives={},
    )
    return outcome, download, workers


def test_resolves_existing_revisit_empty_and_slash_without_playback():
    identity = make_capt()
    capture = ParsedCapture(identity, "text/html")

    outcome, download, workers = resolve(
        capture, existing=frozenset({identity})
    )
    assert (outcome.kind, download) == (CaptureKind.EXISTING, None)
    workers.download.assert_not_called()

    representative = stored_from_playback(playback(identity))
    later = make_capt(ts="20040616000000")
    outcome, download, workers = resolve(
        ParsedCapture(later, "text/html"),
        representatives={revisit_group_key(later): representative},
    )
    assert (outcome.kind, download) == (CaptureKind.REVISIT, None)
    workers.download.assert_not_called()

    empty = make_capt(digest=EMPTY_PAYLOAD_DIGEST)
    outcome, download, workers = resolve(ParsedCapture(empty, "text/plain"))
    assert (outcome.kind, download) == (CaptureKind.EMPTY, None)
    workers.download.assert_not_called()

    redirect = make_capt(url="http://example.org/path", status="301")
    outcome, download, workers = resolve(
        ParsedCapture(redirect, "text/html"),
        group_urls=(redirect.original_url, "http://example.org/path/"),
    )
    assert (outcome.kind, download) == (CaptureKind.SLASH_REDIRECT, None)
    workers.download.assert_not_called()

def test_resolves_download_variants_and_failure_semantically():
    identity = make_capt()
    capture = ParsedCapture(identity, "text/html")

    base = playback(identity)
    for result in (
        base,
        replace(base, substituted=True),
        replace(base, digest_matched=False),
    ):
        downloaded = DownloadOutcome(result, None, 1, 0.25, ())
        outcome, returned, workers = resolve(capture, downloaded=downloaded)
        assert outcome.kind is CaptureKind.DOWNLOADED
        assert outcome.playback is result
        assert returned is downloaded
        workers.download.assert_called_once_with(identity)

    failure = UnresolvedFailure(
        identity,
        FailureCategory.UNAVAILABLE,
        "unavailable",
    )
    downloaded = DownloadOutcome(None, failure, 1, 0.25, ("unavailable",))
    outcome, returned, _workers = resolve(capture, downloaded=downloaded)
    assert outcome.kind is CaptureKind.FAILURE
    assert outcome.failure is failure
    assert returned is downloaded
