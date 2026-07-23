import base64
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
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
    return paths.urlkey_warc_path(urlkey, root=tmp_path / "warcs")


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


def test_different_cdx_status_fetches_then_semantically_deduplicates(
    tmp_path,
):
    first = capture(captured="20170101000000", statuscode=200)
    second = capture(captured="20180101000000", statuscode=201)
    client = FakeClient(
        {
            first: memento_for(first, payload=b"same", status_code=200),
            second: memento_for(second, payload=b"same", status_code=200),
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


def test_genuine_scheme_and_www_redirect_is_written(tmp_path):
    selected = capture(
        original="http://www.example.com/",
        statuscode=301,
        payload=b"redirect",
    )
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

    export.export_group(URLKEY, [selected], target, client)

    response = read_records(target)[1]
    assert response.rec_type == "response"
    assert response.http_headers.get_statuscode() == "301"
    assert (
        response.http_headers.get_header("Location")
        == "https://example.com/"
    )


def test_skippable_wayback_errors_warn_and_unrelated_capture_continues(
    tmp_path,
    capsys,
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

    export.export_group(URLKEY, captures, target, client)

    assert client.calls == captures
    assert [record.rec_type for record in read_records(target)] == [
        "warcinfo",
        "response",
    ]
    assert capsys.readouterr().err.count("WARNING skipped") == 5


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


def test_second_rate_limit_is_fatal(tmp_path, monkeypatch):
    selected = capture()
    client = FakeClient(
        {
            selected: [
                RateLimitError(None, 1),
                RateLimitError(None, 2),
            ]
        }
    )
    sleeps = []
    monkeypatch.setattr(retrieval.time, "sleep", sleeps.append)

    with pytest.raises(RateLimitError):
        export.export_group(
            URLKEY,
            [selected],
            output_path(tmp_path),
            client,
        )
    assert sleeps == [1]


def test_all_skipped_group_creates_no_file(tmp_path):
    selected = capture()
    client = FakeClient(
        {selected: MementoPlaybackError("capture unavailable")}
    )
    target = output_path(tmp_path)

    export.export_group(URLKEY, [selected], target, client)

    assert not target.exists()
    assert not (tmp_path / "warcs").exists()


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


def test_dedup_maps_are_scoped_to_each_group(tmp_path):
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
    output_paths = paths.preflight_paths(
        groups,
        root=tmp_path / "warcs",
    )
    client = FakeClient(
        {
            first: memento_for(first),
            second: memento_for(second),
        }
    )

    export.export_all(groups, output_paths, client)

    assert client.calls == [first, second]
    assert all(
        [record.rec_type for record in read_records(path)]
        == ["warcinfo", "response"]
        for path in output_paths.values()
    )


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


def test_open_new_warc_exclusively_rejects_existing_target(tmp_path):
    target = tmp_path / "existing.warc.gz"
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        warc.open_new_warc(target)


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
