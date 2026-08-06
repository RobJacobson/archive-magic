import base64
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from requests.exceptions import (
    ChunkedEncodingError,
    ContentDecodingError,
    ReadTimeout,
)
from urllib3.exceptions import IncompleteRead, ProtocolError
from warcio.archiveiterator import ArchiveIterator
from wayback import CdxRecord
from wayback.exceptions import (
    BlockedByRobotsError,
    BlockedSiteError,
    MementoPlaybackError,
    NoMementoError,
    RateLimitError,
    UnexpectedResponseFormat,
    WaybackRetryError,
)

from archive_magic_fetch import (
    collection_paths,
    downloads,
    warc_files,
    warc_records,
)


URLKEY = "com,example)/resource"


def payload_digest(payload):
    encoded = base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii")
    return f"sha1:{encoded}"


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def capture(
    *,
    original="https://example.com/resource",
    captured="20170101000000",
    payload=b"payload",
    digest=None,
    statuscode=200,
    urlkey=URLKEY,
):
    if digest is None:
        digest = payload_digest(payload).split(":", 1)[1]
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype="text/plain",
        statuscode=statuscode,
        digest=digest,
        length=len(payload),
    )


class FakeMemento:
    def __init__(
        self,
        *,
        url,
        captured,
        payload=b"payload",
        status_code=200,
        headers=None,
    ):
        self.url = url
        self.timestamp = timestamp(captured)
        self.status_code = status_code
        self.memento_url = (
            f"https://web.archive.org/web/{captured}id_/{url}"
        )
        self.headers = headers or {"Content-Type": "text/plain"}
        self.content = payload
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def get_memento(self, selected, **kwargs):
        self.calls.append(selected)
        outcome = self.outcomes[selected]
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_retries_immediate(monkeypatch):
    delays = []
    monkeypatch.setattr(downloads, "sleep_seconds", delays.append)
    return delays


def memento_for(
    selected,
    *,
    url=None,
    captured=None,
    payload=b"payload",
    status_code=None,
    headers=None,
):
    return FakeMemento(
        url=url or selected.original,
        captured=captured
        or selected.timestamp.astimezone(timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        ),
        payload=payload,
        status_code=(
            status_code
            if status_code is not None
            else selected.statuscode or 200
        ),
        headers=headers,
    )


def read_records(path):
    with path.open("rb") as stream:
        return list(ArchiveIterator(stream))


def output_path(tmp_path, urlkey=URLKEY):
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    return collection_paths.preferred_warc_path(
        urlkey,
        "https://example.com/",
        layout,
    )


def test_matching_cdx_digests_write_one_response_and_one_revisit(tmp_path):
    first = capture(
        captured="20170101000000",
    )
    second = capture(
        captured="20180101000000",
    )
    client = FakeClient(
        {
            first: memento_for(
                first,
            ),
            second: memento_for(
                second,
            ),
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    assert summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        revisits=1,
    )
    assert records[1].rec_headers.get_header(
        "WARC-Target-URI"
    ) == first.original
    assert records[2].rec_headers.get_header(
        "WARC-Target-URI"
    ) == second.original
    assert records[2].rec_headers.get_header(
        "WARC-Refers-To"
    ) == records[1].rec_headers.get_header("WARC-Record-ID")
    assert all(
        record.rec_headers.get_header("CDX-Payload-Digest")
        == payload_digest(b"payload")
        for record in records[1:]
    )


def test_value_equal_source_rows_collapse_to_one_logical_capture(tmp_path):
    first = capture()
    second = capture()
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        FakeClient({first: memento_for(first)}),
    )

    assert first == second
    assert first is not second
    assert summary.selected == 1
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]


def test_value_equal_failed_rows_are_attempted_and_counted_once(tmp_path):
    first = capture()
    second = capture()
    target = output_path(tmp_path)
    client = FakeClient({first: MementoPlaybackError("unavailable")})

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first]
    assert summary == warc_files.WarcCounts(
        selected=1,
        playback_failures=1,
    )


def test_same_url_and_timestamp_with_different_digests_remain_distinct(tmp_path):
    first = capture(payload=b"first")
    second = capture(payload=b"second")
    target = output_path(tmp_path)
    client = FakeClient(
        {
            first: memento_for(first, payload=b"first"),
            second: memento_for(second, payload=b"second"),
        }
    )

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first, second]
    assert summary == warc_files.WarcCounts(selected=2, responses=2)


def test_empty_playback_body_writes_zero_length_response(tmp_path, capsys):
    selected = capture(payload=b"")
    target = output_path(tmp_path)
    client = FakeClient(
        {selected: memento_for(selected, payload=b"")}
    )

    summary = warc_files.build_url_history(URLKEY, [selected], target, client)

    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
    ]
    assert summary == warc_files.WarcCounts(selected=1, responses=1)
    assert records[1].content_stream().read() == b""
    assert records[1].rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == payload_digest(b"")
    assert "empty playback body" not in capsys.readouterr().out


def test_unexpected_empty_playback_body_remains_failure(tmp_path, capsys):
    selected = capture(payload=b"expected")
    target = output_path(tmp_path)
    client = FakeClient(
        {selected: memento_for(selected, payload=b"")}
    )

    summary = warc_files.build_url_history(URLKEY, [selected], target, client)

    assert summary == warc_files.WarcCounts(
        selected=1,
        playback_failures=1,
    )
    assert not target.exists()
    assert "empty playback body" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("memento_kwargs", "message"),
    [
        (
            {"captured": "20170101000001"},
            "CDX timestamp 20170101000000 but playback returned",
        ),
        (
            {"url": "https://example.com/different"},
            "CDX URL https://example.com/resource but playback returned",
        ),
    ],
)
def test_non_exact_playback_metadata_is_rejected(
    tmp_path,
    capsys,
    memento_kwargs,
    message,
):
    selected = capture()
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient({selected: memento_for(selected, **memento_kwargs)}),
    )

    assert summary == warc_files.WarcCounts(
        selected=1,
        playback_failures=1,
    )
    assert not target.exists()
    assert message in capsys.readouterr().out


def test_failed_capture_is_recovered_from_later_matching_digest(
    tmp_path,
    capsys,
):
    first = capture(captured="20170101000000")
    second = capture(captured="20180101000000")
    client = FakeClient(
        {
            first: MementoPlaybackError("capture unavailable"),
            second: memento_for(second),
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first, second]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    records = read_records(target)
    assert records[2].rec_headers.get_header("WARC-Date") == (
        "2017-01-01T00:00:00Z"
    )
    assert records[2].rec_headers.get_header(
        "CDX-Payload-Digest"
    ) == payload_digest(b"payload")
    assert summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        revisits=1,
        digest_recoveries=1,
    )
    assert capsys.readouterr().out == ""


def test_warc_result_lists_failed_capture_view_url(tmp_path):
    selected = capture()
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )

    result = warc_files.build_warc_files(
        {selected.urlkey: [selected]},
        FakeClient(
            {selected: MementoPlaybackError("capture unavailable")}
        ),
        layout=layout,
    )

    assert result.failed_capture_urls == (selected.view_url,)


def test_matching_bodies_are_stored_as_full_responses(tmp_path):
    first = capture(captured="20170101000000", digest="-")
    second = capture(captured="20180101000000", digest="malformed")
    client = FakeClient(
        {
            first: memento_for(first, payload=b"same"),
            second: memento_for(second, payload=b"same"),
        }
    )
    target = output_path(tmp_path)

    warc_files.build_url_history(URLKEY, [first, second], target, client)

    assert client.calls == [first, second]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert all(
        record.rec_headers.get_header("CDX-Payload-Digest") == "-"
        for record in records[1:]
    )


def test_invalid_digest_failure_is_not_recovered(tmp_path):
    first = capture(captured="20170101000000", digest="-")
    second = capture(captured="20180101000000", digest="-")
    target = output_path(tmp_path)
    client = FakeClient(
        {
            first: MementoPlaybackError("capture unavailable"),
            second: memento_for(second),
        }
    )

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first, second]
    assert summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        playback_failures=1,
    )


def test_distinct_statuses_are_preserved(tmp_path):
    first = capture(captured="20170101000000", statuscode=200)
    second = capture(captured="20180101000000", statuscode=404)
    client = FakeClient(
        {
            first: memento_for(first, payload=b"same", status_code=200),
            second: memento_for(second, payload=b"same", status_code=404),
        }
    )
    target = output_path(tmp_path)

    warc_files.build_url_history(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    assert [
        record.http_headers.get_statuscode() for record in records[1:]
    ] == ["200", "404"]


def test_status_substitution_is_skipped(
    tmp_path,
    capsys,
):
    first = capture(
        captured="20170101000000",
        statuscode=200,
        payload=b"first",
    )
    second = capture(
        captured="20180101000000",
        statuscode=201,
        payload=b"second",
    )
    third = capture(
        captured="20190101000000",
        statuscode=201,
        payload=b"third",
    )
    client = FakeClient(
        {
            first: memento_for(first, payload=b"first", status_code=200),
            second: memento_for(second, payload=b"second", status_code=200),
            third: memento_for(third, payload=b"third", status_code=201),
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second, third],
        target,
        client,
    )

    assert client.calls == [first, second, third]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert summary.responses == 2
    assert summary.playback_failures == 1
    assert "CDX status 201 but playback returned 200" in capsys.readouterr().out


def test_statusless_captures_with_matching_digest_use_revisit(tmp_path):
    first = capture(captured="20170101000000", statuscode=None)
    second = capture(captured="20180101000000", statuscode=None)
    client = FakeClient(
        {
            first: memento_for(first, status_code=204),
            second: memento_for(second, status_code=204),
        }
    )
    target = output_path(tmp_path)

    warc_files.build_url_history(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert records[1].http_headers.get_statuscode() == "204"
    assert records[2].http_headers.get_statuscode() == "204"


@pytest.mark.parametrize("statuscode", [300, 301, 308, 399])
def test_known_cdx_3xx_is_written_as_response(
    tmp_path,
    statuscode,
):
    selected = capture(
        original="http://www.example.com/",
        statuscode=statuscode,
        payload=b"redirect",
    )
    client = FakeClient(
        {
            selected: memento_for(
                selected,
                payload=b"redirect",
                status_code=statuscode,
                headers={"Location": "https://example.com/"},
            )
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == [selected]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
    ]
    assert records[1].http_headers.get_statuscode() == str(statuscode)
    assert (
        records[1].http_headers.get_header("Location")
        == "https://example.com/"
    )
    with target.open("rb") as stream:
        iterator = iter(ArchiveIterator(stream))
        next(iterator)
        assert next(iterator).content_stream().read() == b"redirect"
    assert summary == warc_files.WarcCounts(selected=1, responses=1)


def test_warc_build_does_not_return_redirect_targets(tmp_path):
    selected = capture(
        original="http://source.test/path/start",
        statuscode=301,
        payload=b"redirect",
        urlkey="test,source)/path/start",
    )
    client = FakeClient(
        {
            selected: memento_for(
                selected,
                payload=b"redirect",
                status_code=301,
                headers={"Location": "../landing#section"},
            )
        }
    )
    layout = collection_paths.collection_paths(
        "source.test/*",
        root=tmp_path / "archives",
    )

    result = warc_files.build_warc_files(
        {selected.urlkey: [selected]},
        client,
        layout=layout,
    )

    assert not hasattr(result, "redirect_targets")
    assert result.warc_counts.responses == 1


def test_retrieved_statusless_redirect_is_written_as_response(tmp_path):
    selected = capture(statuscode=None, payload=b"redirect")
    client = FakeClient(
        {
            selected: memento_for(
                selected,
                payload=b"redirect",
                status_code=301,
                headers={"Location": "https://example.com/"},
            )
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == [selected]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
    ]
    assert records[1].http_headers.get_statuscode() == "301"
    assert (
        records[1].http_headers.get_header("Location")
        == "https://example.com/"
    )
    assert summary == warc_files.WarcCounts(selected=1, responses=1)


def test_matching_cdx_redirect_digests_write_full_responses(tmp_path):
    first = capture(
        original="https://example.com/first",
        captured="20170101000000",
        statuscode=301,
        payload=b"redirect",
    )
    second = capture(
        original="https://example.com/second",
        captured="20180101000000",
        statuscode=301,
        payload=b"redirect",
    )
    client = FakeClient(
        {
            first: memento_for(
                first,
                payload=b"redirect",
                headers={"Location": "https://example.com/one"},
            ),
            second: memento_for(
                second,
                payload=b"redirect",
                headers={"Location": "https://example.com/two"},
            ),
        }
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first, second]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert [
        record.http_headers.get_header("Location")
        for record in records[1:]
    ] == ["https://example.com/one", "https://example.com/two"]
    assert summary == warc_files.WarcCounts(selected=2, responses=2)


def test_failed_redirect_is_not_digest_recovered(tmp_path):
    first = capture(captured="20170101000000", statuscode=301)
    second = capture(captured="20180101000000", statuscode=301)
    target = output_path(tmp_path)
    client = FakeClient(
        {
            first: MementoPlaybackError("redirect unavailable"),
            second: memento_for(
                second,
                status_code=301,
                headers={"Location": "https://example.com/target"},
            ),
        }
    )

    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [first, second]
    assert summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        playback_failures=1,
    )


def test_skippable_wayback_errors_warn_and_unrelated_capture_continues(
    tmp_path,
    capsys,
    monkeypatch,
):
    failures = [
        MementoPlaybackError("playback failed"),
        NoMementoError("no memento"),
        BlockedByRobotsError("blocked by robots"),
        BlockedSiteError("blocked site"),
        WaybackRetryError(0, 1, ReadTimeout("timeout")),
    ]
    captures = [
        capture(
            captured=f"20170{index}01000000",
            digest=payload_digest(f"stored-{index}".encode()),
        )
        for index in range(1, 7)
    ]
    outcomes = {
        selected: error
        for selected, error in zip(captures[:-1], failures, strict=True)
    }
    outcomes[captures[-1]] = memento_for(captures[-1])
    client = FakeClient(outcomes)
    target = output_path(tmp_path)
    make_retries_immediate(monkeypatch)

    warc_files.build_url_history(URLKEY, captures, target, client, retries=2)

    assert client.calls[:4] == captures[:4]
    assert client.calls[4:-1] == [
        captures[4]
    ] * 3
    assert client.calls[-1] == captures[-1]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    output = capsys.readouterr().out
    assert "playback failed" in output
    assert "blocked by robots" in output
    assert "timeout" in output


def test_content_decoding_error_warns_once_when_raw_recovery_is_unavailable(
    tmp_path,
    capsys,
):
    selected = capture()
    client = FakeClient(
        {selected: ContentDecodingError("incorrect gzip header")}
    )
    target = output_path(tmp_path)

    summary = warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == [selected]
    assert not target.exists()
    assert summary.playback_failures == 1
    assert summary.invalid_content_encoding_failures == 1
    warning = capsys.readouterr().out
    assert selected.view_url in warning
    assert "decode failed" in warning
    assert "incorrect gzip header" in warning
    assert "raw recovery digest mismatch" in warning
    assert "original Wayback replay could not be decoded" not in warning
    assert "failed during playback" not in warning
    # URL on its own line, detail indented beneath it.
    assert f"  {selected.view_url}\n    " in warning
    assert "\n    " in warning and "decode failed" in warning.split("\n    ", 1)[1]


def test_repeated_truncated_response_warns_early_and_is_categorized(
    tmp_path,
    capsys,
    monkeypatch,
):
    selected = capture()
    truncated = [
        WaybackRetryError(
            1,
            0.2,
            ChunkedEncodingError(
                ProtocolError(
                    "Connection broken",
                    IncompleteRead(130810, 144219),
                )
            ),
        )
        for _ in range(downloads.REPEATED_TRUNCATION_ATTEMPTS)
    ]
    client = FakeClient({selected: truncated})
    make_retries_immediate(monkeypatch)

    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        output_path(tmp_path),
        client,
    )

    assert len(client.calls) == downloads.REPEATED_TRUNCATION_ATTEMPTS
    assert summary.playback_failures == 1
    assert summary.truncated_response_failures == 1
    warning = capsys.readouterr().out
    assert "retrying after incomplete response" in warning
    assert "truncated after 2 attempts over" in warning
    assert "130,810/275,029 bytes" in warning
    assert "IncompleteRead" not in warning
    assert "truncated Wayback response after" not in warning


def test_unexpected_response_format_is_fatal(tmp_path):
    selected = capture()
    client = FakeClient(
        {selected: UnexpectedResponseFormat("malformed response")}
    )

    with pytest.raises(UnexpectedResponseFormat):
        warc_files.build_url_history(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )


def test_repeated_rate_limit_is_bounded_and_skips_capture(
    tmp_path,
    monkeypatch,
    capsys,
):
    selected = capture()
    rate_limits = 3
    client = FakeClient(
        {
            selected: [
                RateLimitError(None, attempt)
                for attempt in range(1, rate_limits + 1)
            ]
        }
    )
    delays = make_retries_immediate(monkeypatch)

    target = output_path(tmp_path)
    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        client,
        retries=2,
    )

    assert delays == [10, 20]
    assert summary.responses == 0
    assert summary.playback_failures == 1
    assert not target.exists()
    output = capsys.readouterr()
    assert output.out.count("\n  retry ") == rate_limits - 1
    assert "after 3 attempts" in output.out


def test_all_skipped_group_creates_no_file(tmp_path):
    selected = capture()
    client = FakeClient(
        {selected: MementoPlaybackError("capture unavailable")}
    )
    target = output_path(tmp_path)

    warc_files.build_url_history(URLKEY, [selected], target, client)

    assert not target.exists()
    assert not (tmp_path / "archives").exists()


def test_local_open_failure_is_fatal(tmp_path, monkeypatch, capsys):
    selected = capture()
    client = FakeClient({selected: memento_for(selected)})

    def fail_open(path, warc_filename=None):
        raise OSError("disk unavailable")

    monkeypatch.setattr(warc_files, "open_new_warc", fail_open)

    with pytest.raises(OSError, match="disk unavailable"):
        warc_files.build_url_history(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )
    assert "WARNING" not in capsys.readouterr().err


def test_existing_temporary_is_replaced_on_rebuild(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.parent.mkdir(parents=True)
    temporary.write_bytes(b"stale rebuild")
    client = FakeClient({selected: memento_for(selected)})

    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        client,
    )

    assert summary.responses == 1
    assert not temporary.exists()
    assert target.is_file()
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]


def test_warc_serialization_failure_is_fatal(tmp_path, monkeypatch):
    selected = capture()
    client = FakeClient({selected: memento_for(selected)})

    def fail_write(writer, response):
        raise RuntimeError("cannot serialize")

    monkeypatch.setattr(warc_files, "write_response", fail_write)

    with pytest.raises(RuntimeError, match="cannot serialize"):
        warc_files.build_url_history(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )


def test_groups_are_built_independently(tmp_path, capsys):
    first = capture(
        original="https://example.com/a",
        urlkey="com,example)/a",
    )
    second = capture(
        original="https://example.com/b",
        urlkey="com,example)/b",
    )
    groups = {
        first.urlkey: [first],
        second.urlkey: [second],
    }
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    client = FakeClient(
        {
            first: memento_for(first),
            second: memento_for(second),
        }
    )

    result = warc_files.build_warc_files(groups, client, layout=layout)

    assert client.calls == [first, second]
    assert result.warc_counts == warc_files.WarcCounts(
        selected=2,
        responses=2,
    )
    assert all(
        [record.rec_type for record in read_records(path)]
        == ["warcinfo", "response"]
        for path in result.built_warcs
    )
    output = capsys.readouterr().out
    assert "WARC files: building 2 with 8 workers" in output
    assert output.count(
        " responses, 0 revisits (0 recovered), 0 failed"
    ) == 2


def test_colliding_groups_share_one_warc(tmp_path):
    trailing = capture(
        original="https://example.com/posts/",
        urlkey="com,example)/posts/",
        captured="20180101000000",
    )
    explicit = capture(
        original="https://example.com/posts/index",
        urlkey="com,example)/posts/index",
        captured="20170101000000",
    )
    groups = {
        trailing.urlkey: [trailing],
        explicit.urlkey: [explicit],
    }
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    client = FakeClient(
        {
            trailing: memento_for(trailing, payload=b"same"),
            explicit: memento_for(explicit, payload=b"same"),
        }
    )

    result = warc_files.build_warc_files(groups, client, layout=layout)

    assert len(
        collection_paths.allocate_warc_paths(
            {("example.com", key): value for key, value in groups.items()},
            layout,
        )
    ) == 1
    assert client.calls == [trailing, explicit]
    assert result.built_warcs == (
        layout.archive_root / "example.com" / "posts" / "index.warc.gz",
    )
    assert [record.rec_type for record in read_records(result.built_warcs[0])] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert result.warc_counts == warc_files.WarcCounts(
        selected=2,
        responses=2,
    )


def test_file_directory_conflict_is_built_to_ancestor_warc(tmp_path):
    parent = capture(
        original="https://example.com/foo",
        urlkey="com,example)/foo",
    )
    descendant = capture(
        original="https://example.com/foo.warc.gz/bar",
        urlkey="com,example)/foo.warc.gz/bar",
    )
    groups = {
        descendant.urlkey: [descendant],
        parent.urlkey: [parent],
    }
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    result = warc_files.build_warc_files(
        groups,
        FakeClient(
            {
                parent: memento_for(parent),
                descendant: memento_for(descendant),
            }
        ),
        layout=layout,
    )

    assert result.built_warcs == (
        layout.archive_root / "example.com" / "foo.warc.gz",
    )
    assert [record.rec_type for record in read_records(result.built_warcs[0])] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_all_failed_batch_returns_no_final_warc(tmp_path):
    selected = capture()
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    result = warc_files.build_warc_files(
        groups,
        FakeClient(
            {selected: MementoPlaybackError("capture unavailable")}
        ),
        layout=layout,
    )

    assert result.built_warcs == ()
    assert result.warc_counts == warc_files.WarcCounts(
        selected=1,
        playback_failures=1,
    )
    assert not collection_paths.preferred_warc_path(
        selected.urlkey,
        selected.original,
        layout,
    ).exists()


def test_generated_file_is_parseable_gzip_warc_1_0(tmp_path):
    selected = capture()
    target = output_path(tmp_path)

    warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient({selected: memento_for(selected)}),
    )

    records = read_records(target)
    assert records[0].rec_headers.protocol == "WARC/1.0"
    assert records[1].rec_headers.protocol == "WARC/1.0"
    assert records[1].rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == payload_digest(b"payload")


def test_original_percent_escapes_are_preserved_in_warc_and_output(
    tmp_path,
    capsys,
):
    selected = capture(
        original=(
            "http://www.wecanstopthehate.org/"
            "%7Bfiledir_2%7DIB_9.pdf"
        ),
    )
    target = output_path(tmp_path)

    warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient(
            {
                selected: memento_for(
                    selected,
                    url=(
                        "http://www.wecanstopthehate.org/"
                        "%257Bfiledir_2%257DIB_9.pdf"
                    ),
                )
            }
        ),
    )

    response = read_records(target)[1]
    assert response.rec_headers.get_header(
        "WARC-Target-URI"
    ) == selected.original
    assert "%257B" not in response.rec_headers.get_header(
        "WARC-Target-URI"
    )
    assert capsys.readouterr().out == ""


def test_history_downloads_unique_responses_and_preserves_write_order(
    tmp_path,
    capsys,
):
    first = capture(captured="20170101000000", payload=b"alpha")
    duplicate = capture(captured="20180101000000", payload=b"alpha")
    second = capture(
        captured="20190101000000",
        payload=b"beta",
        digest=payload_digest(b"beta").split(":", 1)[1],
    )
    target = output_path(tmp_path)
    outcomes = {
        first: memento_for(first, payload=b"alpha"),
        duplicate: memento_for(duplicate, payload=b"alpha"),
        second: memento_for(second, payload=b"beta"),
    }
    client = FakeClient(outcomes)

    summary = warc_files.build_url_history(
        URLKEY,
        [first, duplicate, second],
        target,
        client,
    )

    assert summary.responses == 2
    assert summary.revisits == 1
    assert client.calls == [first, second]

    assert capsys.readouterr().out == ""

    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
        "response",
    ]
    assert records[1].rec_headers.get_header("WARC-Date") == "2017-01-01T00:00:00Z"
    assert records[2].rec_headers.get_header("WARC-Date") == "2018-01-01T00:00:00Z"
    assert records[3].rec_headers.get_header("WARC-Date") == "2019-01-01T00:00:00Z"


def test_warc_all_runs_different_warc_batches_concurrently(
    tmp_path,
    capsys,
    monkeypatch,
):
    first = capture(
        original="https://example.com/a",
        captured="20170101000000",
        payload=b"alpha",
        urlkey="com,example)/a",
    )
    second = capture(
        original="https://example.com/b",
        captured="20180101000000",
        payload=b"beta",
        urlkey="com,example)/b",
    )
    outcomes = {
        first: memento_for(first, payload=b"alpha"),
        second: memento_for(second, payload=b"beta"),
    }
    first_started = threading.Event()
    release_first = threading.Event()
    second_finished = threading.Event()
    second_reported = threading.Event()
    created_clients = []
    real_print = print

    def record_print(*args, **kwargs):
        real_print(*args, **kwargs)
        if args and "http://web.archive.org/web/*/https://example.com/b" in str(
            args[0]
        ):
            second_reported.set()

    monkeypatch.setattr(warc_files, "print", record_print, raising=False)

    class FactoryClient:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_memento(self, selected, **kwargs):
            self.calls.append(selected)
            if selected is first:
                first_started.set()
                release_first.wait(timeout=10)
            if selected is second:
                second_finished.set()
            return outcomes[selected]

    def factory():
        client = FactoryClient()
        created_clients.append(client)
        return client

    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    groups = {
        first.urlkey: [first],
        second.urlkey: [second],
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            warc_files.build_warc_files,
            groups,
            FactoryClient(),
            layout=layout,
            client_factory=factory,
            worker_count=2,
        )
        assert first_started.wait(timeout=2)
        assert second_finished.wait(timeout=2)
        assert second_reported.wait(timeout=2)
        assert not future.done()
        release_first.set()
        result = future.result(timeout=2)

    assert result.warc_counts.responses == 2
    assert len(created_clients) == 2
    worker_calls = [call for client in created_clients for call in client.calls]
    assert set(worker_calls) == {first, second}
    assert result.built_warcs == (
        layout.archive_root / "example.com" / "a.warc.gz",
        layout.archive_root / "example.com" / "b.warc.gz",
    )
    output = capsys.readouterr().out
    completed = [
        line for line in output.splitlines() if line.startswith("[")
    ]
    assert "http://web.archive.org/web/*/https://example.com/b" in completed[0]
    assert "http://web.archive.org/web/*/https://example.com/a" in completed[1]


def test_thread_client_pool_reuses_and_closes_client():
    created = []

    class Client:
        def __init__(self):
            self.exited = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.exited = True

    def factory():
        client = Client()
        created.append(client)
        return client

    pool = downloads.ThreadClientPool(factory)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(pool.get).result()
        second = executor.submit(pool.get).result()
    pool.close()

    assert first is second
    assert created == [first]
    assert first.exited is True


def test_histories_sharing_one_warc_remain_serial(tmp_path):
    first = capture(
        original="https://example.com/posts/",
        urlkey="com,example)/posts/",
    )
    second = capture(
        original="https://example.com/posts/index",
        urlkey="com,example)/posts/index",
    )
    first_finished = threading.Event()

    class OrderedClient(FakeClient):
        def get_memento(self, selected, **kwargs):
            if selected is second:
                assert first_finished.is_set()
            result = super().get_memento(selected, **kwargs)
            if selected is first:
                first_finished.set()
            return result

    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    client = OrderedClient(
        {
            first: memento_for(first),
            second: memento_for(second),
        }
    )

    warc_files.build_warc_files(
        {first.urlkey: [first], second.urlkey: [second]},
        client,
        layout=layout,
        worker_count=8,
    )

    assert client.calls == [first, second]


def test_one_warc_batch_is_opened_validated_and_published_once(
    tmp_path,
    monkeypatch,
):
    first = capture(
        original="https://example.com/posts/",
        urlkey="com,example)/posts/",
    )
    second = capture(
        original="https://example.com/posts/index",
        urlkey="com,example)/posts/index",
    )
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    opened = []
    validated = []
    published = []
    real_open = warc_files.open_new_warc
    real_validate = warc_files.validate_warc
    real_replace = warc_files.os.replace

    def record_open(*args, **kwargs):
        opened.append(args[0])
        return real_open(*args, **kwargs)

    def record_validation(path):
        validated.append(path)
        return real_validate(path)

    def record_publication(source, destination):
        published.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(warc_files, "open_new_warc", record_open)
    monkeypatch.setattr(warc_files, "validate_warc", record_validation)
    monkeypatch.setattr(warc_files.os, "replace", record_publication)

    result = warc_files.build_warc_files(
        {first.urlkey: [first], second.urlkey: [second]},
        FakeClient(
            {
                first: memento_for(first),
                second: memento_for(second),
            }
        ),
        layout=layout,
    )

    assert len(result.built_warcs) == 1
    assert opened == validated
    assert len(opened) == 1
    assert published == [(opened[0], result.built_warcs[0])]


def test_open_new_warc_exclusively_rejects_existing_target(tmp_path):
    target = tmp_path / "existing.warc.gz"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        warc_records.open_new_warc(target)


def test_unreadable_existing_target_is_fatal_and_untouched(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    client = FakeClient({selected: memento_for(selected)})

    with pytest.raises(ValueError, match="cannot inventory existing WARC"):
        warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == []
    assert target.read_bytes() == b"existing"


def test_unchanged_rebuild_reuses_full_responses_without_wayback(
    tmp_path,
    capsys,
):
    selected = capture()
    target = output_path(tmp_path)
    first_client = FakeClient({selected: memento_for(selected)})
    warc_files.build_url_history(URLKEY, [selected], target, first_client)
    original = target.read_bytes()
    capsys.readouterr()

    second_client = FakeClient({})
    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        second_client,
    )

    assert second_client.calls == []
    assert summary == warc_files.WarcCounts(selected=1, responses=1)
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
    ]
    assert records[0].rec_headers.get_header("WARC-Filename") == target.name
    assert target.read_bytes() == original
    assert not target.with_name(target.name + ".tmp").exists()
    assert capsys.readouterr().out == ""


def test_cdx_digest_mismatch_is_stable_cache_identity(tmp_path):
    selected = capture(digest="A" * 32)
    target = output_path(tmp_path)

    warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient({selected: memento_for(selected, payload=b"actual")}),
    )
    record = read_records(target)[1]
    assert record.rec_headers.get_header(
        "CDX-Payload-Digest"
    ) == "sha1:" + "A" * 32
    assert record.rec_headers.get_header(
        "WARC-Payload-Digest"
    ) == payload_digest(b"actual")

    client = FakeClient({})
    summary = warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        client,
    )

    assert client.calls == []
    assert summary == warc_files.WarcCounts(selected=1, responses=1)


def test_revisit_body_lookup_uses_actual_warc_digest(tmp_path):
    first = capture(captured="20170101000000", digest="A" * 32)
    second = capture(captured="20180101000000", digest="A" * 32)
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        FakeClient({first: memento_for(first, payload=b"actual")}),
    )

    client = FakeClient({})
    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == []
    assert summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        revisits=1,
    )


def test_rebuild_reuses_old_response_and_fetches_only_missing_capture(
    tmp_path,
):
    first = capture(captured="20170101000000", payload=b"alpha")
    second = capture(captured="20180101000000", payload=b"beta")
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [first],
        target,
        FakeClient({first: memento_for(first, payload=b"alpha")}),
    )

    client = FakeClient(
        {second: memento_for(second, payload=b"beta")}
    )
    summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [second]
    assert summary == warc_files.WarcCounts(selected=2, responses=2)
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert [
        record.rec_headers.get_header("WARC-Date")
        for record in records[1:]
    ] == [
        "2017-01-01T00:00:00Z",
        "2018-01-01T00:00:00Z",
    ]


def test_rebuild_preserves_old_capture_absent_from_current_cdx(tmp_path):
    old = capture(captured="20170101000000", payload=b"old")
    new = capture(captured="20190101000000", payload=b"new")
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [old],
        target,
        FakeClient({old: memento_for(old, payload=b"old")}),
    )

    client = FakeClient({new: memento_for(new, payload=b"new")})
    summary = warc_files.build_url_history(URLKEY, [new], target, client)

    assert client.calls == [new]
    assert summary == warc_files.WarcCounts(selected=1, responses=2)
    records = read_records(target)
    assert [
        record.rec_headers.get_header("WARC-Date")
        for record in records
        if record.rec_type in {"response", "revisit"}
    ] == ["2017-01-01T00:00:00Z", "2019-01-01T00:00:00Z"]


def test_normalized_url_spelling_reuses_existing_capture(tmp_path):
    first = capture(original="http://EXAMPLE.com:80/resource")
    equivalent = capture(original="http://example.com/resource")
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [first],
        target,
        FakeClient({first: memento_for(first)}),
    )

    client = FakeClient({})
    warc_files.build_url_history(URLKEY, [equivalent], target, client)

    assert client.calls == []
    assert len(
        [record for record in read_records(target) if record.rec_type == "response"]
    ) == 1


def test_partial_warc_is_published_and_backfilled_on_next_run(tmp_path):
    first = capture(captured="20170101000000", payload=b"alpha")
    second = capture(captured="20180101000000", payload=b"beta")
    target = output_path(tmp_path)

    first_summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        FakeClient(
            {
                first: memento_for(first, payload=b"alpha"),
                second: MementoPlaybackError("temporarily unavailable"),
            }
        ),
    )

    assert first_summary == warc_files.WarcCounts(
        selected=2,
        responses=1,
        playback_failures=1,
    )
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]

    client = FakeClient(
        {second: memento_for(second, payload=b"beta")}
    )
    second_summary = warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [second]
    assert second_summary == warc_files.WarcCounts(
        selected=2,
        responses=2,
    )
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_all_failed_rebuild_preserves_and_reports_existing_final(
    tmp_path,
):
    previous = capture(captured="20170101000000", payload=b"previous")
    selected = capture(captured="20180101000000", payload=b"selected")
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [previous],
        target,
        FakeClient(
            {previous: memento_for(previous, payload=b"previous")}
        ),
    )
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )

    result = warc_files.build_warc_files(
        {URLKEY: [selected]},
        FakeClient(
            {selected: MementoPlaybackError("temporarily unavailable")}
        ),
        layout=layout,
    )

    assert result.built_warcs == (target,)
    assert result.warc_counts.playback_failures == 1
    records = read_records(target)
    assert [record.rec_type for record in records] == ["warcinfo", "response"]
    assert records[1].rec_headers.get_header("WARC-Date") == (
        "2017-01-01T00:00:00Z"
    )


def test_old_exact_revisit_is_preserved_without_wayback(
    tmp_path,
):
    first = capture(captured="20170101000000", payload=b"same")
    second = capture(captured="20180101000000", payload=b"same")
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [first, second],
        target,
        FakeClient({first: memento_for(first, payload=b"same")}),
    )
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]

    client = FakeClient(
        {second: memento_for(second, payload=b"same")}
    )
    warc_files.build_url_history(URLKEY, [second], target, client)

    assert client.calls == []
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_invalid_cached_payload_digest_is_fatal_and_untouched(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    retrieved = downloads.DownloadedCapture(
        body=b"payload",
        url=selected.original,
        capture_date="2017-01-01T00:00:00Z",
        source_uri=selected.raw_url,
        status_code=200,
        headers=(("Content-Type", "text/plain"),),
    )
    stream, writer = warc_records.open_new_warc(target)
    response = retrieved.to_warc_record(
        cdx_payload_digest=selected.digest,
        target_url=selected.original,
    )
    response.rec_headers.replace_header(
        "WARC-Payload-Digest",
        "sha1:" + "A" * 32,
    )
    writer.write_record(response)
    stream.close()

    original = target.read_bytes()
    client = FakeClient({selected: memento_for(selected)})
    with pytest.raises(ValueError, match="cannot inventory existing WARC"):
        warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == []
    assert target.read_bytes() == original


def test_cached_record_without_cdx_digest_is_fatal_and_untouched(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    retrieved = downloads.DownloadedCapture(
        body=b"payload",
        url=selected.original,
        capture_date="2017-01-01T00:00:00Z",
        source_uri=selected.raw_url,
        status_code=200,
        headers=(("Content-Type", "text/plain"),),
    )
    stream, writer = warc_records.open_new_warc(target)
    response = retrieved.to_warc_record(
        cdx_payload_digest=selected.digest,
        target_url=selected.original,
    )
    response.rec_headers.remove_header("CDX-Payload-Digest")
    writer.write_record(response)
    stream.close()

    original = target.read_bytes()
    client = FakeClient({selected: memento_for(selected)})
    with pytest.raises(
        ValueError,
        match="missing CDX-Payload-Digest",
    ):
        warc_files.build_url_history(URLKEY, [selected], target, client)

    assert client.calls == []
    assert target.read_bytes() == original


def test_ambiguous_cached_cdx_digest_disables_revisit_reuse(tmp_path):
    first = capture(captured="20170101000000", digest="A" * 32)
    second = capture(captured="20180101000000", digest="A" * 32)
    third = capture(captured="20190101000000", digest="A" * 32)
    target = output_path(tmp_path)
    stream, writer = warc_records.open_new_warc(target)
    for selected, body in ((first, b"alpha"), (second, b"beta")):
        response = downloads.DownloadedCapture(
            body=body,
            url=selected.original,
            capture_date=warc_records.timestamp_to_warc_date(
                selected.timestamp
            ),
            source_uri=selected.raw_url,
            status_code=200,
            headers=(("Content-Type", "text/plain"),),
        ).to_warc_record(
            cdx_payload_digest=selected.digest,
            target_url=selected.original,
        )
        writer.write_record(response)
    stream.close()

    client = FakeClient(
        {third: memento_for(third, payload=b"gamma")}
    )
    summary = warc_files.build_url_history(
        URLKEY,
        [third],
        target,
        client,
    )

    assert client.calls == [third]
    assert summary == warc_files.WarcCounts(selected=1, responses=3)
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
        "response",
    ]


def test_temporary_validation_failure_preserves_existing_warc(
    tmp_path,
    monkeypatch,
):
    selected = capture()
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient({selected: memento_for(selected)}),
    )
    original = target.read_bytes()
    added = capture(captured="20180101000000", payload=b"added")

    def fail_validation(path):
        raise ValueError("temporary is invalid")

    monkeypatch.setattr(warc_files, "validate_warc", fail_validation)

    with pytest.raises(ValueError, match="temporary is invalid"):
        warc_files.build_url_history(
            URLKEY,
            [selected, added],
            target,
            FakeClient({added: memento_for(added, payload=b"added")}),
        )

    assert target.read_bytes() == original
    assert not target.with_name(target.name + ".tmp").exists()


def test_superset_check_failure_preserves_existing_warc(tmp_path, monkeypatch):
    selected = capture()
    target = output_path(tmp_path)
    warc_files.build_url_history(
        URLKEY,
        [selected],
        target,
        FakeClient({selected: memento_for(selected)}),
    )
    original = target.read_bytes()
    added = capture(captured="20180101000000", payload=b"added")
    real_inventory = warc_records.ExistingWarcCache.inventory

    class MissingInventory:
        identities = frozenset()

    def inventory(path):
        if path.name.endswith(".tmp"):
            return MissingInventory()
        return real_inventory(path)

    monkeypatch.setattr(
        warc_files.ExistingWarcCache,
        "inventory",
        staticmethod(inventory),
    )

    with pytest.raises(ValueError, match="would lose 1 prior logical capture"):
        warc_files.build_url_history(
            URLKEY,
            [selected, added],
            target,
            FakeClient({added: memento_for(added, payload=b"added")}),
        )

    assert target.read_bytes() == original
    assert not target.with_name(target.name + ".tmp").exists()


def test_timestamp_to_warc_date_normalizes_aware_non_utc_datetime():
    value = datetime(
        2020,
        1,
        2,
        1,
        4,
        5,
        999999,
        tzinfo=timezone(timedelta(hours=-2)),
    )

    assert warc_records.timestamp_to_warc_date(value) == "2020-01-02T03:04:05Z"


def test_cdx_timestamp_normalizes_aware_non_utc_datetime():
    value = datetime(
        2020,
        1,
        2,
        8,
        34,
        5,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )

    assert warc_files._cdx_timestamp(value) == "20200102030405"


def test_timestamp_to_warc_date_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        warc_records.timestamp_to_warc_date(datetime(2020, 1, 1))
