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

from archive_magic_fetch import export, paths, retrieval, warc


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
    monkeypatch.setattr(retrieval, "sleep_seconds", delays.append)
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    return paths.preferred_warc_path(urlkey, layout)


def test_matching_cdx_digests_write_one_response_and_one_revisit(tmp_path):
    first = capture(
        original="https://cdx.example/first",
        captured="20170101000000",
    )
    second = capture(
        original="https://cdx.example/second",
        captured="20180101000000",
    )
    client = FakeClient(
        {
            first: memento_for(
                first,
                url="https://played.example/first",
                captured="20170102030405",
            ),
            second: memento_for(
                second,
                url="https://played.example/second",
                captured="20180102030405",
            ),
        }
    )
    target = output_path(tmp_path)

    summary = export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    assert summary == export.ExportSummary(
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


def test_empty_playback_body_writes_zero_length_response(tmp_path, capsys):
    selected = capture(payload=b"")
    target = output_path(tmp_path)
    client = FakeClient(
        {selected: memento_for(selected, payload=b"")}
    )

    summary = export.export_group(URLKEY, [selected], target, client)

    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
    ]
    assert summary == export.ExportSummary(selected=1, responses=1)
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

    summary = export.export_group(URLKEY, [selected], target, client)

    assert summary == export.ExportSummary(
        selected=1,
        playback_failures=1,
    )
    assert not target.exists()
    assert "empty playback body" in capsys.readouterr().out


def test_failed_capture_does_not_prevent_later_capture(
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

    export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first, second]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    assert "capture unavailable" in capsys.readouterr().out


def test_export_result_lists_failed_capture_view_url(tmp_path):
    selected = capture()
    bucket = paths.WarcBucket(
        tmp_path / "failed.warc.gz",
        (selected.urlkey,),
    )

    result = export.export_all(
        {selected.urlkey: [selected]},
        (bucket,),
        FakeClient(
            {selected: MementoPlaybackError("capture unavailable")}
        ),
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

    export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first, second]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]


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

    export.export_group(URLKEY, [first, second], target, client)

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

    summary = export.export_group(
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

    export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert records[1].http_headers.get_statuscode() == "204"
    assert records[2].http_headers.get_statuscode() == "204"


@pytest.mark.parametrize("statuscode", [300, 301, 308, 399])
def test_known_cdx_3xx_is_omitted_without_playback(
    tmp_path,
    statuscode,
):
    selected = capture(
        original="http://www.example.com/",
        statuscode=statuscode,
        payload=b"redirect",
    )
    client = FakeClient({})
    target = output_path(tmp_path)

    summary = export.export_group(URLKEY, [selected], target, client)

    assert client.calls == []
    assert not target.exists()
    assert summary.selected == 1
    assert summary.redirects_omitted == 1


def test_retrieved_statusless_redirect_is_omitted(tmp_path):
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

    summary = export.export_group(URLKEY, [selected], target, client)

    assert client.calls == [selected]
    assert not target.exists()
    assert summary.selected == 1
    assert summary.redirects_omitted == 1
    assert summary.playback_failures == 0


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

    export.export_group(URLKEY, captures, target, client, retries=2)

    assert client.calls[:4] == captures[:4]
    assert client.calls[4:-1] == [
        captures[4]
    ] * 3
    assert client.calls[-1] == captures[-1]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    assert capsys.readouterr().out.count("WARNING:") == 5


def test_persistent_content_decoding_error_warns_once_and_skips(
    tmp_path,
    capsys,
):
    selected = capture()
    client = FakeClient(
        {
            selected: [
                ContentDecodingError("incorrect gzip header"),
                ContentDecodingError("still incorrect under identity"),
            ]
        }
    )
    client.session = type(
        "Session",
        (),
        {
            "headers": {"Accept-Encoding": "gzip, deflate"},
            "reset": lambda self: None,
        },
    )()
    target = output_path(tmp_path)

    summary = export.export_group(URLKEY, [selected], target, client)

    assert client.calls == [selected, selected]
    assert not target.exists()
    assert summary.playback_failures == 1
    assert summary.invalid_content_encoding_failures == 1
    warning = capsys.readouterr().out
    assert warning.count("WARNING:") == 1
    assert (
        "original Wayback replay could not be decoded by the HTTP client"
        in warning
    )
    assert (
        "retrying with Accept-Encoding: identity also failed"
        in warning
    )
    assert "still incorrect under identity" in warning


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
        for _ in range(retrieval.REPEATED_TRUNCATION_ATTEMPTS)
    ]
    client = FakeClient({selected: truncated})
    make_retries_immediate(monkeypatch)

    summary = export.export_group(
        URLKEY,
        [selected],
        output_path(tmp_path),
        client,
    )

    assert len(client.calls) == retrieval.REPEATED_TRUNCATION_ATTEMPTS
    assert summary.playback_failures == 1
    assert summary.truncated_response_failures == 1
    warning = capsys.readouterr().out
    assert "truncated Wayback response after 3 attempts over" in warning
    assert "received 130,810 of 275,029 bytes" in warning
    warning_lines = [
        line for line in warning.splitlines() if "WARNING:" in line
    ]
    assert all("IncompleteRead" not in line for line in warning_lines)


def test_unexpected_response_format_is_fatal(tmp_path):
    selected = capture()
    client = FakeClient(
        {selected: UnexpectedResponseFormat("malformed response")}
    )

    with pytest.raises(UnexpectedResponseFormat):
        export.export_group(
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
    summary = export.export_group(
        URLKEY,
        [selected],
        target,
        client,
        retries=2,
    )

    assert delays == [2, 4]
    assert summary.responses == 0
    assert summary.playback_failures == 1
    assert not target.exists()
    output = capsys.readouterr()
    assert output.out.count(" : retry ") == rate_limits - 1
    assert output.out.count("WARNING:") == 1


def test_all_skipped_group_creates_no_file(tmp_path):
    selected = capture()
    client = FakeClient(
        {selected: MementoPlaybackError("capture unavailable")}
    )
    target = output_path(tmp_path)

    export.export_group(URLKEY, [selected], target, client)

    assert not target.exists()
    assert not (tmp_path / "archives").exists()


def test_local_open_failure_is_fatal(tmp_path, monkeypatch, capsys):
    selected = capture()
    client = FakeClient({selected: memento_for(selected)})

    def fail_open(path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(export, "open_new_warc", fail_open)

    with pytest.raises(OSError, match="disk unavailable"):
        export.export_group(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )
    assert "WARNING" not in capsys.readouterr().err


def test_warc_serialization_failure_is_fatal(tmp_path, monkeypatch):
    selected = capture()
    client = FakeClient({selected: memento_for(selected)})

    def fail_write(writer, response):
        raise RuntimeError("cannot serialize")

    monkeypatch.setattr(export, "write_response", fail_write)

    with pytest.raises(RuntimeError, match="cannot serialize"):
        export.export_group(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )


def test_groups_are_exported_independently(tmp_path, capsys):
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_layout(groups, layout)
    client = FakeClient(
        {
            first: memento_for(first),
            second: memento_for(second),
        }
    )

    result = export.export_all(groups, plan.buckets, client)

    assert client.calls == [first, second]
    assert result.summary == export.ExportSummary(
        selected=2,
        responses=2,
    )
    assert all(
        [record.rec_type for record in read_records(path)]
        == ["warcinfo", "response"]
        for path in result.created_warcs
    )
    assert capsys.readouterr().out.count("[completed ") == 2
    export.print_summary(result.summary)
    assert capsys.readouterr().out.endswith(
        "Summary: 2 selected for warc (all); 2 responses; "
        "0 revisits; "
        "0 redirects omitted; 0 playback failures\n"
    )


def test_summary_includes_playback_failure_categories(capsys):
    summary = export.ExportSummary(
        selected=8,
        responses=5,
        playback_failures=3,
        invalid_content_encoding_failures=1,
        truncated_response_failures=1,
    )

    export.print_summary(summary)

    assert capsys.readouterr().out == (
        "Summary: 8 selected for warc (all); 5 responses; "
        "0 revisits; "
        "0 redirects omitted; 3 playback failures "
        "(1 invalid content encoding, 1 truncated response, 1 other)\n"
    )


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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_layout(groups, layout)
    client = FakeClient(
        {
            trailing: memento_for(trailing, payload=b"same"),
            explicit: memento_for(explicit, payload=b"same"),
        }
    )

    result = export.export_all(groups, plan.buckets, client)

    assert len(plan.buckets) == 1
    assert client.calls == [trailing, explicit]
    assert result.created_warcs == (
        layout.archive_root / "posts" / "index.warc.gz",
    )
    assert [record.rec_type for record in read_records(result.created_warcs[0])] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert result.summary == export.ExportSummary(
        selected=2,
        responses=2,
    )


def test_file_directory_conflict_is_exported_to_ancestor_warc(tmp_path):
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_layout(groups, layout)

    result = export.export_all(
        groups,
        plan.buckets,
        FakeClient(
            {
                parent: memento_for(parent),
                descendant: memento_for(descendant),
            }
        ),
    )

    assert result.created_warcs == (layout.archive_root / "foo.warc.gz",)
    assert [record.rec_type for record in read_records(result.created_warcs[0])] == [
        "warcinfo",
        "response",
        "response",
    ]


def test_all_skipped_bucket_returns_no_created_warc(tmp_path):
    selected = capture(statuscode=301)
    groups = {selected.urlkey: [selected]}
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_layout(groups, layout)

    result = export.export_all(groups, plan.buckets, FakeClient({}))

    assert result.created_warcs == ()
    assert result.summary == export.ExportSummary(
        selected=1,
        redirects_omitted=1,
    )
    assert not plan.buckets[0].path.exists()


def test_generated_file_is_parseable_gzip_warc_1_0(tmp_path):
    selected = capture()
    target = output_path(tmp_path)

    export.export_group(
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

    export.export_group(
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
    output = capsys.readouterr().out
    assert (
        "[completed 1/1] "
        "wecanstopthehate.org/%7Bfiledir_2%7DIB_9.pdf"
    ) in output


def test_group_fetches_unique_representatives_and_preserves_write_order(
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

    summary = export.export_group(
        URLKEY,
        [first, duplicate, second],
        target,
        client,
    )

    assert summary.responses == 2
    assert summary.revisits == 1
    assert client.calls == [first, second]

    output = capsys.readouterr().out
    outcomes = [
        line.rsplit(" : ", 1)[1]
        for line in output.splitlines()
        if line.startswith("https://web.archive.org/")
    ]
    assert outcomes == ["wrote response", "wrote revisit", "wrote response"]

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


def test_export_all_runs_different_warc_buckets_concurrently(
    tmp_path,
    capsys,
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
    created_clients = []

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

    buckets = (
        paths.WarcBucket(tmp_path / "a.warc.gz", (first.urlkey,)),
        paths.WarcBucket(tmp_path / "b.warc.gz", (second.urlkey,)),
    )
    groups = {
        first.urlkey: [first],
        second.urlkey: [second],
    }

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            export.export_all,
            groups,
            buckets,
            FactoryClient(),
            client_factory=factory,
            concurrency=2,
        )
        assert first_started.wait(timeout=2)
        assert second_finished.wait(timeout=2)
        assert not future.done()
        release_first.set()
        result = future.result(timeout=2)

    assert result.summary.responses == 2
    assert len(created_clients) == 2
    worker_calls = [call for client in created_clients for call in client.calls]
    assert set(worker_calls) == {first, second}
    assert set(result.created_warcs) == {
        buckets[0].path,
        buckets[1].path,
    }
    output = capsys.readouterr().out
    completed = [
        line for line in output.splitlines() if line.startswith("[completed ")
    ]
    assert {line.rsplit("] ", 1)[1] for line in completed} == {
        "example.com/a",
        "example.com/b",
    }


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

    pool = export._ThreadClientPool(factory)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(pool.get).result()
        second = executor.submit(pool.get).result()
    pool.close()

    assert first is second
    assert created == [first]
    assert first.exited is True


def test_groups_sharing_one_warc_bucket_remain_serial(tmp_path):
    first = capture(
        original="https://example.com/a",
        urlkey="com,example)/a",
    )
    second = capture(
        original="https://example.com/b",
        urlkey="com,example)/b",
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

    bucket = paths.WarcBucket(
        tmp_path / "shared.warc.gz",
        (first.urlkey, second.urlkey),
    )
    client = OrderedClient(
        {
            first: memento_for(first),
            second: memento_for(second),
        }
    )

    export.export_all(
        {first.urlkey: [first], second.urlkey: [second]},
        (bucket,),
        client,
        concurrency=8,
    )

    assert client.calls == [first, second]


def test_open_new_warc_exclusively_rejects_existing_target(tmp_path):
    target = tmp_path / "existing.warc.gz"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        warc.open_new_warc(target)


def test_export_rejects_existing_target(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        export.export_group(
            URLKEY,
            [selected],
            target,
            FakeClient({selected: memento_for(selected)}),
        )


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

    assert warc.timestamp_to_warc_date(value) == "2020-01-02T03:04:05Z"


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

    assert export._cdx_timestamp(value) == "20200102030405"


def test_timestamp_to_warc_date_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        warc.timestamp_to_warc_date(datetime(2020, 1, 1))
