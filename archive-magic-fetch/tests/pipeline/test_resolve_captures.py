"""Sequential per-URL success tracking, without advance candidate selection."""

from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from archive_magic_fetch.archive.format import (
    EMPTY_PAYLOAD_DIGEST,
    MISSING_CDX_PAYLOAD_DIGEST,
)
from archive_magic_fetch.archive.identity import revisit_group_key
from archive_magic_fetch.archive.inventory_collection import stored_from_capture
from archive_magic_fetch.models import (
    CaptureKind,
    CaptureRef,
    DownloadOutcome,
    FailureCategory,
    UnresolvedFailure,
)
from archive_magic_fetch.pipeline.resolve_captures import _process_url_group
from helpers import make_capt, playback


def success(identity, *, matched=True):
    result = replace(playback(identity), digest_matched=matched)
    return DownloadOutcome(result, None, 1, 0.25, ())


def failure(identity):
    return DownloadOutcome(
        None,
        UnresolvedFailure(identity, FailureCategory.UNAVAILABLE, "unavailable"),
        1,
        0.25,
        ("unavailable",),
    )


def resolve_group(
    identities, outcomes=(), *, existing=frozenset(), representatives=None
):
    workers = MagicMock()
    workers.download.side_effect = outcomes
    result = _process_url_group(
        [CaptureRef(identity, "text/html") for identity in identities],
        download=workers.download,
        existing_identities=existing,
        existing_representatives=representatives or {},
    )
    return result, workers


def test_failure_and_mismatch_leave_digest_eligible_until_success():
    captures = [make_capt(ts=f"2004060{day}000000") for day in range(1, 5)]
    outcome, workers = resolve_group(
        captures,
        [
            failure(captures[0]),
            success(captures[1], matched=False),
            success(captures[2]),
        ],
    )
    assert [
        call.args[0].identity for call in workers.download.call_args_list
    ] == captures[:3]
    assert [item.kind for item in outcome.captures] == [
        CaptureKind.FAILURE,
        CaptureKind.DOWNLOADED,
        CaptureKind.DOWNLOADED,
        CaptureKind.REVISIT,
    ]
    assert outcome.captures[3].representative.identity == captures[2]
    assert outcome.attempts == 3
    assert outcome.categories == ("unavailable",)


def test_interleaved_digests_are_decided_as_captures_are_visited():
    a1 = make_capt(ts="20040601000000")
    b1 = make_capt(ts="20040602000000", digest="sha1:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    a2 = replace(a1, timestamp="20040603000000")
    b2 = replace(b1, timestamp="20040604000000")
    a3 = replace(a1, timestamp="20040605000000")
    outcome, workers = resolve_group(
        [a1, b1, a2, b2, a3], [failure(a1), success(b1), success(a2)]
    )
    assert [call.args[0].identity for call in workers.download.call_args_list] == [
        a1,
        b1,
        a2,
    ]
    assert outcome.captures[3].representative.identity == b1
    assert outcome.captures[4].representative.identity == a2


def test_identical_digest_on_another_url_still_downloads():
    a = make_capt(url="http://example.org/a", ts="20040601000000")
    b = make_capt(url="http://example.org/b", ts="20040602000000")
    representatives = {revisit_group_key(a): stored_from_capture(playback(a))}
    outcome, workers = resolve_group([b], [success(b)], representatives=representatives)
    workers.download.assert_called_once_with(CaptureRef(b, "text/html"))
    assert outcome.captures[0].kind is CaptureKind.DOWNLOADED
    assert list(representatives) == [revisit_group_key(a)]


def test_existing_success_seeds_revisits_without_network_or_shared_mutations():
    first = make_capt(ts="20040601000000")
    later = replace(first, timestamp="20040602000000")
    representatives = {revisit_group_key(first): stored_from_capture(playback(first))}
    outcome, workers = resolve_group(
        [first, later],
        existing=frozenset({first}),
        representatives=representatives,
    )
    workers.download.assert_not_called()
    assert [item.kind for item in outcome.captures] == [
        CaptureKind.EXISTING,
        CaptureKind.REVISIT,
    ]
    assert outcome.captures[1].representative.identity == first


def test_revisit_cannot_reference_another_year():
    from archive_magic_fetch.archive.inventory_collection import revisit_from_stored

    first = make_capt(ts="20040601000000")
    later = replace(first, timestamp="20050601000000")
    with pytest.raises(ValueError, match="same year"):
        revisit_from_stored(later, stored_from_capture(playback(first)))


def test_later_stored_response_does_not_hide_earlier_failed_capture():
    earlier = make_capt(ts="20040601000000")
    stored = replace(earlier, timestamp="20040602000000")
    later = replace(earlier, timestamp="20040603000000")
    representatives = {revisit_group_key(stored): stored_from_capture(playback(stored))}
    outcome, workers = resolve_group(
        [earlier, stored, later],
        [failure(earlier)],
        existing=frozenset({stored}),
        representatives=representatives,
    )
    workers.download.assert_called_once_with(CaptureRef(earlier, "text/html"))
    assert [item.kind for item in outcome.captures] == [
        CaptureKind.FAILURE,
        CaptureKind.EXISTING,
        CaptureKind.REVISIT,
    ]
    assert outcome.captures[2].representative.identity == stored


def test_missing_digests_do_not_share_a_success_entry():
    first = make_capt(ts="20040601000000", digest=MISSING_CDX_PAYLOAD_DIGEST)
    second = replace(first, timestamp="20040602000000")
    outcome, workers = resolve_group([first, second], [success(first), success(second)])
    assert workers.download.call_count == 2
    assert all(item.kind is CaptureKind.DOWNLOADED for item in outcome.captures)


@pytest.mark.parametrize(
    "status,digest", [("200", EMPTY_PAYLOAD_DIGEST), ("301", EMPTY_PAYLOAD_DIGEST)]
)
def test_unseen_empty_and_redirect_captures_require_successful_playback(status, digest):
    first = make_capt(
        ts="20040601000000", url="http://example.org/path", status=status, digest=digest
    )
    second = replace(first, timestamp="20040602000000")
    third = replace(first, timestamp="20040603000000")
    outcome, workers = resolve_group(
        [first, second, third], [failure(first), success(second)]
    )
    assert [call.args[0].identity for call in workers.download.call_args_list] == [
        first,
        second,
    ]
    assert [item.kind for item in outcome.captures] == [
        CaptureKind.FAILURE,
        CaptureKind.DOWNLOADED,
        CaptureKind.REVISIT,
    ]
