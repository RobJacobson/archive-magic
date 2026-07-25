import base64
import hashlib
import threading
from datetime import datetime, timedelta, timezone

import pytest
from requests.exceptions import ChunkedEncodingError, ContentDecodingError
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


def test_normalize_digest_accepts_raw_and_prefixed_sha1():
    raw = payload_digest(b"payload").split(":", 1)[1]

    assert export.normalize_digest(raw.lower()) == f"sha1:{raw}"
    assert export.normalize_digest(f"SHA1:{raw.lower()}") == f"sha1:{raw}"


@pytest.mark.parametrize(
    "value",
    [None, "", "-", "md5:AAAAAAAA", "sha1:short", "!" * 32],
)
def test_normalize_digest_rejects_unusable_values(value):
    assert export.normalize_digest(value) is None


def test_source_signature_shortcut_uses_cdx_identity(tmp_path):
    first = capture(
        original="https://cdx.example/first",
        captured="20170101000000",
    )
    second = capture(
        original="https://cdx.example/second",
        captured="20180101000000",
    )
    first_memento = memento_for(
        first,
        url="https://played.example/first",
        captured="20170102030405",
    )
    client = FakeClient({first: first_memento})
    target = output_path(tmp_path)

    export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
    ]
    response, revisit = records[1:]
    assert response.rec_headers.get_header(
        "WARC-Target-URI"
    ) == "https://played.example/first"
    assert (
        response.rec_headers.get_header("WARC-Date")
        == "2017-01-02T03:04:05Z"
    )
    assert revisit.rec_headers.get_header(
        "WARC-Target-URI"
    ) == second.original
    assert (
        revisit.rec_headers.get_header("WARC-Date")
        == "2018-01-01T00:00:00Z"
    )
    assert revisit.rec_headers.get_header(
        "WARC-Refers-To"
    ) == response.rec_headers.get_header("WARC-Record-ID")
    assert revisit.rec_headers.get_header(
        "WARC-Refers-To-Target-URI"
    ) == "https://played.example/first"
    assert (
        revisit.rec_headers.get_header("WARC-Refers-To-Date")
        == "2017-01-02T03:04:05Z"
    )
    assert revisit.content_stream().read() == b""


def test_semantic_duplicate_uses_memento_identity_and_original_canonical(
    tmp_path,
):
    first = capture(
        original="https://cdx.example/a",
        captured="20170101000000",
        digest=payload_digest(b"stored-a"),
    )
    second = capture(
        original="https://cdx.example/b",
        captured="20180101000000",
        digest=payload_digest(b"stored-b"),
    )
    third = capture(
        original="https://cdx.example/c",
        captured="20190101000000",
        digest=payload_digest(b"stored-b"),
    )
    client = FakeClient(
        {
            first: memento_for(
                first,
                url="https://played.example/a",
                captured="20170102000000",
                payload=b"same semantic body",
            ),
            second: memento_for(
                second,
                url="https://played.example/b",
                captured="20180102000000",
                payload=b"same semantic body",
            ),
        }
    )
    target = output_path(tmp_path)

    export.export_group(URLKEY, [first, second, third], target, client)

    assert client.calls == [first, second]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "revisit",
        "revisit",
    ]
    response, semantic_revisit, source_revisit = records[1:]
    assert semantic_revisit.rec_headers.get_header(
        "WARC-Target-URI"
    ) == "https://played.example/b"
    assert (
        semantic_revisit.rec_headers.get_header("WARC-Date")
        == "2018-01-02T00:00:00Z"
    )
    assert source_revisit.rec_headers.get_header(
        "WARC-Target-URI"
    ) == third.original
    assert (
        source_revisit.rec_headers.get_header("WARC-Date")
        == "2019-01-01T00:00:00Z"
    )
    canonical_id = response.rec_headers.get_header("WARC-Record-ID")
    assert {
        semantic_revisit.rec_headers.get_header("WARC-Refers-To"),
        source_revisit.rec_headers.get_header("WARC-Refers-To"),
    } == {canonical_id}


def test_failed_first_occurrence_does_not_seed_source_map(
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
    assert "capture unavailable" in capsys.readouterr().err


def test_missing_digests_still_participate_in_semantic_dedup(tmp_path):
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
        "revisit",
    ]


def test_cdx_status_and_actual_status_are_integer_dedup_keys(tmp_path):
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

    assert client.calls == [first, second]
    records = read_records(target)
    assert [record.rec_type for record in records] == [
        "warcinfo",
        "response",
        "response",
    ]
    assert [
        record.http_headers.get_statuscode() for record in records[1:]
    ] == ["200", "404"]


def test_status_substitution_is_skipped_and_does_not_seed_dedup(
    tmp_path,
    capsys,
):
    first = capture(captured="20170101000000", statuscode=200)
    second = capture(captured="20180101000000", statuscode=201)
    third = capture(captured="20190101000000", statuscode=201)
    client = FakeClient(
        {
            first: memento_for(first, payload=b"same", status_code=200),
            second: memento_for(second, payload=b"same", status_code=200),
            third: memento_for(third, payload=b"different", status_code=201),
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
    assert "CDX status 201 but playback returned 200" in capsys.readouterr().err


def test_statusless_revisit_reuses_successful_digest(tmp_path):
    first = capture(captured="20170101000000", statuscode=200)
    revisit = capture(captured="20180101000000", statuscode=None)
    client = FakeClient({first: memento_for(first)})
    target = output_path(tmp_path)

    export.export_group(URLKEY, [first, revisit], target, client)

    assert client.calls == [first]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "revisit",
    ]


def test_statusless_first_occurrence_is_retrieved(tmp_path):
    first = capture(captured="20170101000000", statuscode=None)
    second = capture(captured="20180101000000", statuscode=None)
    client = FakeClient({first: memento_for(first, status_code=204)})
    target = output_path(tmp_path)

    export.export_group(URLKEY, [first, second], target, client)

    assert client.calls == [first]
    records = read_records(target)
    assert records[1].http_headers.get_statuscode() == "204"
    assert records[2].rec_type == "revisit"


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
        WaybackRetryError(2, 1, RuntimeError("timeout")),
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
    original = retrieval.RateLimitGate.after_throttle

    def immediate(self, generation, *, retry_after=None):
        return original(self, generation, retry_after=0)

    monkeypatch.setattr(
        retrieval.RateLimitGate,
        "after_throttle",
        immediate,
    )

    export.export_group(URLKEY, captures, target, client)

    assert client.calls[:4] == captures[:4]
    assert client.calls[4:-1] == [
        captures[4]
    ] * retrieval.MAX_THROTTLE_ATTEMPTS
    assert client.calls[-1] == captures[-1]
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    assert capsys.readouterr().err.count("WARNING skipped") == 5


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
    warning = capsys.readouterr().err
    assert warning.count("WARNING skipped") == 1
    assert "invalid Wayback replay response" in warning
    assert (
        "retrying with Accept-Encoding: identity also failed"
        in warning
    )
    assert "incorrect gzip header" not in warning


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
    original = retrieval.RateLimitGate.after_throttle

    def immediate(self, generation, *, retry_after=None):
        return original(self, generation, retry_after=0)

    monkeypatch.setattr(
        retrieval.RateLimitGate,
        "after_throttle",
        immediate,
    )

    summary = export.export_group(
        URLKEY,
        [selected],
        output_path(tmp_path),
        client,
    )

    assert len(client.calls) == retrieval.REPEATED_TRUNCATION_ATTEMPTS
    assert summary.playback_failures == 1
    assert summary.truncated_response_failures == 1
    warning = capsys.readouterr().err
    assert "truncated Wayback response after 3 attempts over" in warning
    assert "received 130,810 of 275,029 bytes" in warning
    assert "IncompleteRead" not in warning


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


def test_repeated_rate_limit_eventually_writes_same_capture(
    tmp_path,
    monkeypatch,
    capsys,
):
    selected = capture()
    rate_limits = retrieval.MAX_THROTTLE_ATTEMPTS + 1
    client = FakeClient(
        {
            selected: [
                RateLimitError(None, attempt)
                for attempt in range(1, rate_limits + 1)
            ]
            + [memento_for(selected)]
        }
    )
    delays = []
    original = retrieval.RateLimitGate.after_throttle

    def immediate(self, generation, *, retry_after=None):
        delays.append(retry_after)
        coordinated = original(self, generation, retry_after=0)
        return None if coordinated is None else retry_after

    monkeypatch.setattr(
        retrieval.RateLimitGate,
        "after_throttle",
        immediate,
    )

    target = output_path(tmp_path)
    summary = export.export_group(
        URLKEY,
        [selected],
        target,
        client,
    )

    assert delays == list(range(1, rate_limits + 1))
    assert summary.responses == 1
    assert summary.playback_failures == 0
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    output = capsys.readouterr()
    assert output.out.count("Rate limited by Internet Archive") == rate_limits
    assert "WARNING skipped" not in output.err


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


def test_dedup_maps_are_scoped_to_each_group(tmp_path, capsys):
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
    assert "Summary:" not in capsys.readouterr().out
    export.print_summary(result.summary)
    assert capsys.readouterr().out.endswith(
        "Summary: 2 selected for warc (all); 2 responses; 0 revisits; "
        "0 already present; 0 redirects omitted; 0 playback failures\n"
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
        "Summary: 8 selected for warc (all); 5 responses; 0 revisits; "
        "0 already present; 0 redirects omitted; 3 playback failures "
        "(1 invalid content encoding, 1 truncated response, 1 other)\n"
    )


def test_colliding_groups_share_one_warc_but_not_deduplication(tmp_path):
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


def test_plan_group_fetches_omits_later_matching_cdx_signatures():
    first = capture(captured="20170101000000", payload=b"one")
    duplicate = capture(captured="20180101000000", payload=b"one")
    other = capture(
        captured="20190101000000",
        payload=b"two",
        digest=payload_digest(b"two").split(":", 1)[1],
    )
    redirect = capture(captured="20200101000000", statuscode=301)

    planned = export.plan_group_fetches([first, duplicate, other, redirect])

    assert planned == [first, other]


def test_concurrent_export_preserves_write_order_and_skips_duplicate_fetch(
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
    created_clients = []
    release_second = threading.Event()
    first_started = threading.Event()

    class FactoryClient:
        def __init__(self, outcomes):
            self.outcomes = outcomes
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_memento(self, selected, **kwargs):
            self.calls.append(selected)
            if selected is first:
                first_started.set()
                release_second.wait(timeout=2)
            outcome = self.outcomes[selected]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    outcomes = {
        first: memento_for(first, payload=b"alpha"),
        second: memento_for(second, payload=b"beta"),
    }

    def factory():
        client = FactoryClient(outcomes)
        created_clients.append(client)
        return client

    cache = retrieval.RetrievalCache()
    main_client = FactoryClient(outcomes)

    def run_export():
        return export.export_group(
            URLKEY,
            [first, duplicate, second],
            target,
            main_client,
            cache=cache,
            client_factory=factory,
            concurrency=2,
        )

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_export)
        assert first_started.wait(timeout=2)
        # Second fetch can complete while the writer still waits on first.
        release_second.set()
        summary = future.result()

    assert summary.responses == 2
    assert summary.revisits == 1
    worker_calls = [call for client in created_clients for call in client.calls]
    assert set(worker_calls) == {first, second}
    assert duplicate not in worker_calls
    assert main_client.calls == []

    output = capsys.readouterr().out
    assert "fetching" not in output
    fetched = [
        line for line in output.splitlines() if line.startswith("Fetched ")
    ]
    assert set(fetched) == {
        f"Fetched 20170101000000 {first.original}",
        f"Fetched 20190101000000 {second.original}",
    }
    wrote = [
        line for line in output.splitlines() if line.startswith("Wrote ")
    ]
    assert wrote == [
        f"Wrote 20170101000000 [{payload_digest(b'alpha')[-8:]}]",
        f"Wrote 20190101000000 [{payload_digest(b'beta')[-8:]}]",
    ]

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


def test_export_all_fetches_later_url_groups_independently(tmp_path):
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
                release_first.wait(timeout=2)
            if selected is second:
                second_finished.set()
            return outcomes[selected]

    def factory():
        client = FactoryClient()
        created_clients.append(client)
        return client

    bucket = paths.WarcBucket(
        tmp_path / "ordered.warc.gz",
        (first.urlkey, second.urlkey),
    )
    groups = {
        first.urlkey: [first],
        second.urlkey: [second],
    }

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            export.export_all,
            groups,
            [bucket],
            FactoryClient(),
            cache=retrieval.RetrievalCache(),
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
    records = read_records(bucket.path)
    assert [
        record.rec_headers.get_header("WARC-Target-URI")
        for record in records[1:]
    ] == [first.original, second.original]


def test_open_new_warc_exclusively_rejects_existing_target(tmp_path):
    target = tmp_path / "existing.warc.gz"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        warc.open_new_warc(target)


def test_rerun_skips_committed_capture_and_appends_only_missing_capture(
    tmp_path,
    capsys,
):
    first = capture(captured="20170101000000", payload=b"first")
    second = capture(
        captured="20180101000000",
        payload=b"second",
        digest=payload_digest(b"second").split(":", 1)[1],
    )
    target = output_path(tmp_path)

    initial = export.export_group(
        URLKEY,
        [first],
        target,
        FakeClient({first: memento_for(first, payload=b"first")}),
    )
    assert initial.responses == 1
    initial_size = target.stat().st_size
    capsys.readouterr()

    client = FakeClient(
        {second: memento_for(second, payload=b"second")}
    )
    resumed = export.export_group(
        URLKEY,
        [first, second],
        target,
        client,
    )

    assert client.calls == [second]
    assert resumed == export.ExportSummary(
        selected=2,
        responses=1,
        already_present=1,
    )
    assert target.stat().st_size > initial_size
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
        "response",
    ]
    output = capsys.readouterr().out
    assert f"Starting {first.original}" in output
    assert "Skipped existing" not in output


def test_rerun_with_every_capture_present_does_not_modify_warc(
    tmp_path,
    capsys,
):
    first = capture()
    second = capture(
        original="http://example.com/resource",
        captured="20180101000000",
        payload=b"second",
    )
    target = output_path(tmp_path)
    export.export_group(
        URLKEY,
        [first, second],
        target,
        FakeClient(
            {
                first: memento_for(first),
                second: memento_for(second, payload=b"second"),
            }
        ),
    )
    original_bytes = target.read_bytes()
    capsys.readouterr()

    summary = export.export_group(
        URLKEY,
        [first, second],
        target,
        FakeClient({}),
    )

    assert summary == export.ExportSummary(
        selected=2,
        already_present=2,
    )
    assert target.read_bytes() == original_bytes
    assert capsys.readouterr().out == (
        f"Skipping {first.original} (2 URL variants) (already captured)\n"
    )


def test_resume_recognizes_legacy_record_without_capture_id(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    stream, writer = warc.open_new_warc(target)
    try:
        response = retrieval.retrieve_response(
            FakeClient({selected: memento_for(selected)}),
            selected,
        )
        warc.write_response(writer, response)
    finally:
        stream.close()
    assert read_records(target)[1].rec_headers.get_header(
        warc.CAPTURE_ID_HEADER
    ) is None

    summary = export.export_group(
        URLKEY,
        [selected],
        target,
        FakeClient({}),
    )

    assert summary == export.ExportSummary(
        selected=1,
        already_present=1,
    )


def test_resume_rejects_non_warc_existing_file(tmp_path):
    selected = capture()
    target = output_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not a WARC")

    with pytest.raises(ValueError, match="malformed existing WARC"):
        export.export_group(URLKEY, [selected], target, FakeClient({}))


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


def test_timestamp_to_warc_date_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        warc.timestamp_to_warc_date(datetime(2020, 1, 1))
