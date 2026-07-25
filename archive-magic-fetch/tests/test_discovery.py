from datetime import datetime, timezone

import pytest
from wayback import CdxRecord
from wayback.exceptions import RateLimitError, UnexpectedResponseFormat

from archive_magic_fetch import cli, discovery


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def record(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    captured="20000101000000",
    statuscode=200,
    digest="A" * 32,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest=digest,
        length=100,
    )


def test_select_latest_prefers_newest_200_over_newer_non_200():
    older_200 = record(captured="20100101000000", statuscode=200)
    newer_404 = record(captured="20200101000000", statuscode=404)
    newer_301 = record(captured="20210101000000", statuscode=301)

    assert discovery.select_latest_capture(
        [older_200, newer_404, newer_301]
    ) is older_200


def test_select_latest_uses_timestamp_not_input_order():
    older_200 = record(captured="20100101000000", statuscode=200)
    newer_200 = record(
        captured="20200101000000",
        statuscode=200,
        digest="B" * 32,
    )

    assert discovery.select_latest_capture([newer_200, older_200]) is newer_200


def test_select_latest_prefers_newest_non_redirect_when_no_200():
    older_404 = record(captured="20100101000000", statuscode=404)
    newer_301 = record(captured="20200101000000", statuscode=301)

    assert discovery.select_latest_capture([newer_301, older_404]) is older_404


def test_select_latest_omits_redirect_only_groups():
    only_301 = record(captured="20200101000000", statuscode=301)
    only_302 = record(captured="20210101000000", statuscode=302)

    assert discovery.select_latest_capture([only_301, only_302]) is None


def test_apply_output_mode_latest_and_none():
    first = record(
        urlkey="com,example)/",
        captured="20100101000000",
        statuscode=404,
    )
    second = record(
        urlkey="com,example)/",
        captured="20200101000000",
        statuscode=200,
    )
    third = record(
        urlkey="com,example)/about",
        original="https://example.com/about",
        captured="20200101000000",
        statuscode=301,
    )
    groups = {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }

    assert discovery.apply_output_mode(groups, "none") == {}
    assert discovery.apply_output_mode(groups, "latest") == {
        first.urlkey: [second],
    }
    assert discovery.apply_output_mode(groups, "all") == {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }


def test_normalize_cdx_search_rewrites_trailing_star_to_explicit_prefix():
    assert discovery.normalize_cdx_search("example.com/*") == (
        "example.com/",
        "prefix",
    )
    assert discovery.normalize_cdx_search("https://example.com/path/*") == (
        "https://example.com/path/",
        "prefix",
    )


def test_normalize_cdx_search_leaves_non_prefix_patterns_unchanged():
    assert discovery.normalize_cdx_search("example.com/") == (
        "example.com/",
        None,
    )
    assert discovery.normalize_cdx_search("*.example.com") == (
        "*.example.com",
        None,
    )


def test_discover_materializes_search_with_explicit_bounds():
    expected = [record()]
    calls = []

    class Client:
        def search(self, url_pattern, **kwargs):
            calls.append((url_pattern, kwargs))
            return iter(expected)

    assert discovery.discover(
        Client(), "example.com/*", "1995", "2020"
    ) == expected
    assert calls == [
        (
            "example.com/",
            {
                "from_date": "1995",
                "to_date": "2020",
                "resolve_revisits": False,
                "match_type": "prefix",
            },
        )
    ]


def test_discover_passes_exact_patterns_without_match_type():
    expected = [record()]
    calls = []

    class Client:
        def search(self, url_pattern, **kwargs):
            calls.append((url_pattern, kwargs))
            return iter(expected)

    assert discovery.discover(
        Client(), "example.com/", "2002", "2002"
    ) == expected
    assert calls == [
        (
            "example.com/",
            {
                "from_date": "2002",
                "to_date": "2002",
                "resolve_revisits": False,
            },
        )
    ]


def test_discover_reports_progress_every_thousand_captures():
    expected = [record() for _ in range(2500)]
    reported = []

    class Client:
        def search(self, *args, **kwargs):
            return iter(expected)

    assert (
        discovery.discover(
            Client(),
            "example.com",
            "1995",
            "2020",
            progress=reported.append,
        )
        == expected
    )
    assert reported == [1000, 2000]


def test_discover_discards_partial_attempt_and_rematerializes_after_rate_limit(
    monkeypatch,
    capsys,
):
    first = record(captured="20000101000000")
    second = record(captured="20010101000000")
    attempts = 0
    sleeps = []
    reported = []

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1

            def results():
                yield first
                if attempts == 1:
                    raise RateLimitError(None, 7)
                yield second

            return results()

    monkeypatch.setattr(discovery.time, "sleep", sleeps.append)

    assert discovery.discover(
        Client(),
        "example.com",
        "1995",
        "2020",
        progress=reported.append,
    ) == [
        first,
        second,
    ]
    assert attempts == 2
    assert sleeps == [7]
    assert reported == []
    assert (
        capsys.readouterr().out
        == "Rate limited during discovery; retrying in 7s...\n"
    )


def test_discover_rate_limit_without_retry_after_uses_sixty_seconds(
    monkeypatch,
    capsys,
):
    attempts = 0
    sleeps = []

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RateLimitError(None, None)
            return iter([])

    monkeypatch.setattr(discovery.time, "sleep", sleeps.append)

    assert discovery.discover(Client(), "example.com", "1995", "2020") == []
    assert sleeps == [60]
    assert (
        capsys.readouterr().out
        == "Rate limited during discovery; retrying in 60s...\n"
    )


def test_discover_second_rate_limit_is_fatal(monkeypatch):
    sleeps = []

    class Client:
        def search(self, *args, **kwargs):
            raise RateLimitError(None, 3)

    monkeypatch.setattr(discovery.time, "sleep", sleeps.append)

    with pytest.raises(RateLimitError):
        discovery.discover(Client(), "example.com", "1995", "2020")
    assert sleeps == [3]


def test_discover_unexpected_response_format_is_fatal():
    class Client:
        def search(self, *args, **kwargs):
            raise UnexpectedResponseFormat("malformed CDX response")

    with pytest.raises(UnexpectedResponseFormat):
        discovery.discover(Client(), "example.com", "1995", "2020")


def test_group_captures_uses_urlkey_and_sorts_by_datetime():
    captures = [
        record(
            urlkey="com,example)/index.html",
            original="https://example.com/index.html",
            captured="20200102000000",
        ),
        record(
            urlkey="com,example)/index.html",
            original="http://example.com/index.html",
            captured="20190101000000",
        ),
        record(
            urlkey="com,example)/other.html",
            original="https://example.com/other.html",
            captured="20180101000000",
        ),
    ]

    grouped = discovery.group_captures(captures)

    assert list(grouped) == [
        "com,example)/index.html",
        "com,example)/other.html",
    ]
    assert [
        capture.timestamp
        for capture in grouped["com,example)/index.html"]
    ] == [
        timestamp("20190101000000"),
        timestamp("20200102000000"),
    ]


def test_group_captures_collapses_value_equal_records():
    capture = record()

    grouped = discovery.group_captures([capture, record()])

    assert grouped[capture.urlkey] == [capture]


def test_group_captures_accepts_upstream_default_port_normalization():
    capture = record(original="http://example.com/")

    grouped = discovery.group_captures([capture])

    assert grouped[capture.urlkey][0].original == "http://example.com/"


class FakeSession:
    def __init__(self, user_agent):
        self.user_agent = user_agent


class FakeClient:
    def __init__(self, session):
        self.session = session
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args):
        self.exited = True


def install_fake_lifecycle(monkeypatch):
    created = {}

    def make_session(*, user_agent):
        session = FakeSession(user_agent)
        created["session"] = session
        return session

    def make_client(*, session):
        client = FakeClient(session)
        created["client"] = client
        return client

    monkeypatch.setattr(cli, "WaybackSession", make_session)
    monkeypatch.setattr(cli, "WaybackClient", make_client)
    return created


def test_cli_owns_one_client_context_and_passes_same_client(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    capture = record()
    groups = {capture.urlkey: [capture]}
    layout = object()
    buckets = (object(),)
    calls = {}

    def fake_discover(client, pattern, start, end, *, progress=None):
        calls["discover"] = (client, pattern, start, end, progress)
        return [capture]

    def fake_export(grouped, planned_buckets, client, *, cache=None):
        calls["export"] = (grouped, planned_buckets, client, cache)
        return type(
            "Result",
            (),
            {
                "summary": type("Summary", (), {"selected": 1})(),
                "created_warcs": (),
            },
        )()

    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")
    monkeypatch.setattr(cli, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(
        cli,
        "save_acquisition",
        lambda captures, **kwargs: calls.setdefault(
            "provenance",
            (captures, kwargs),
        ),
    )
    monkeypatch.setattr(cli, "group_captures", lambda captures: groups)
    monkeypatch.setattr(
        cli,
        "preflight_layout",
        lambda grouped, selected_layout: type(
            "Plan",
            (),
            {"layout": selected_layout, "buckets": buckets},
        )(),
    )
    monkeypatch.setattr(cli, "export_all", fake_export)
    monkeypatch.setattr(
        cli,
        "generate_replay_index",
        lambda created, layout: calls.setdefault(
            "replay",
            (created, layout),
        ),
    )
    monkeypatch.setattr(
        cli,
        "print_summary",
        lambda summary, **kwargs: calls.setdefault(
            "summary",
            (summary, kwargs),
        ),
    )

    assert cli.main(["*.example.com"]) == 0
    client = created["client"]
    assert created["session"].user_agent == cli.USER_AGENT
    assert client.session is created["session"]
    assert client.entered is True
    assert client.exited is True
    assert calls["discover"][:4] == (
        client,
        "*.example.com",
        "1995",
        "20260722123456",
    )
    assert calls["discover"][4] is cli._report_discovery_progress
    assert calls["provenance"][0] == [capture]
    assert calls["export"][0] == groups
    assert calls["export"][1] == buckets
    assert calls["export"][2] == client
    assert calls["export"][3] is not None
    assert calls["replay"] == ((), layout)
    assert calls["summary"][0].selected == 1
    assert calls["summary"][1] == {"warc_mode": "all"}


def test_cli_prints_stage_messages(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    capture = record()
    groups = {capture.urlkey: [capture]}
    layout = object()
    buckets = (object(),)

    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")
    monkeypatch.setattr(cli, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(
        cli,
        "discover",
        lambda *args, **kwargs: [capture],
    )
    monkeypatch.setattr(cli, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "group_captures", lambda captures: groups)
    monkeypatch.setattr(
        cli,
        "preflight_layout",
        lambda grouped, selected_layout: type(
            "Plan",
            (),
            {"layout": selected_layout, "buckets": buckets},
        )(),
    )
    monkeypatch.setattr(
        cli,
        "export_all",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "summary": type("Summary", (), {"selected": 1})(),
                "created_warcs": (),
            },
        )(),
    )
    monkeypatch.setattr(cli, "generate_replay_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "print_summary", lambda summary, **kwargs: None)

    assert cli.main(["*.example.com"]) == 0
    output = capsys.readouterr().out
    assert output == (
        "Discovering captures for *.example.com (1995-20260722123456)\n"
        "Discovered 1 captures\n"
        "Saving source acquisition...\n"
        "Grouping 1 captures...\n"
        "Exporting 1 URL groups to WARC...\n"
        "Building replay index...\n"
    )


def test_cli_passes_explicit_dates(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    calls = {}

    def fake_discover(client, pattern, start, end, *, progress=None):
        calls["discover"] = (client, pattern, start, end, progress)
        return []

    monkeypatch.setattr(cli, "discover", fake_discover)

    assert cli.main(
        ["example.com/*", "--start", "2018", "--end", "20200131"]
    ) == 0
    assert calls["discover"][:4] == (
        created["client"],
        "example.com/*",
        "2018",
        "20200131",
    )
    assert calls["discover"][4] is cli._report_discovery_progress


def test_cli_empty_result_is_success(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)

    def fake_discover(client, pattern, start, end, *, progress=None):
        return []

    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")

    assert cli.main(["example.com/*"]) == 0
    assert capsys.readouterr().out == (
        "Discovering captures for example.com/* (1995-20260722123456)\n"
        "No captures found\n"
    )


def test_cli_fatal_error_returns_one(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(cli, "discover", fail)

    assert cli.main(["example.com/*"]) == 1
    assert capsys.readouterr().err == "ERROR: discovery failed\n"


def test_cli_does_not_print_summary_when_replay_indexing_fails(
    monkeypatch,
    capsys,
):
    install_fake_lifecycle(monkeypatch)
    selected = record()
    layout = object()
    bucket = object()
    monkeypatch.setattr(cli, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: [selected])
    monkeypatch.setattr(cli, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "preflight_layout",
        lambda *args: type(
            "Plan",
            (),
            {"layout": layout, "buckets": (bucket,)},
        )(),
    )
    monkeypatch.setattr(
        cli,
        "export_all",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "summary": type("Summary", (), {})(),
                "created_warcs": (object(),),
            },
        )(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(cli, "generate_replay_index", fail)

    assert cli.main(["example.com/*"]) == 1
    output = capsys.readouterr()
    assert "Summary:" not in output.out
    assert output.err == "ERROR: index failed\n"


def test_cli_retains_published_provenance_after_downstream_failure(
    tmp_path,
    monkeypatch,
):
    install_fake_lifecycle(monkeypatch)
    selected = record()
    monkeypatch.setattr(
        cli,
        "_DEFAULT_OUTPUT_ROOT",
        tmp_path / "archives",
    )
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: [selected])
    monkeypatch.setattr(
        cli,
        "export_all",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {
                "summary": type("Summary", (), {})(),
                "created_warcs": (),
            },
        )(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("downstream failed")

    monkeypatch.setattr(cli, "generate_replay_index", fail)

    assert cli.main(["https://example.com/*"]) == 1
    acquisitions = list(
        (tmp_path / "archives" / "example.com" / "sources" / "wayback").iterdir()
    )
    assert len(acquisitions) == 1
    assert (acquisitions[0] / "captures.cdx.gz").exists()
    assert (acquisitions[0] / "query.json").exists()


def test_cli_defaults_parse_to_warc_all_and_files_none():
    args = cli.parse_args(["example.com/*"])
    assert args.warc == "all"
    assert args.files == "none"
    assert args.rewrite_local is False


def test_cli_rewrite_local_requires_files_mode(capsys):
    assert cli.main(
        ["example.com/*", "--rewrite-local", "--files", "none"]
    ) == 2
    assert (
        "--rewrite-local requires --files latest or --files all"
        in capsys.readouterr().err
    )


def test_cli_rewrite_local_alone_does_not_enable_files(capsys):
    assert cli.main(["example.com/*", "--rewrite-local", "--warc", "none"]) == 2
    assert (
        "--rewrite-local requires --files latest or --files all"
        in capsys.readouterr().err
    )


def test_cli_both_none_is_successful_noop(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise AssertionError("network client should not be created")

    monkeypatch.setattr(cli, "WaybackSession", fail)
    monkeypatch.setattr(cli, "WaybackClient", fail)
    monkeypatch.setattr(cli, "collection_layout", fail)

    assert cli.main(["example.com/*", "--warc", "none", "--files", "none"]) == 0
    assert capsys.readouterr().out == (
        "Nothing to do: both --warc and --files are none\n"
    )


def test_cli_rejects_unapproved_arguments():
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["example.com/*", "--output", "elsewhere"])

    assert error.value.code == 2


def test_report_discovery_progress(capsys):
    cli._report_discovery_progress(2000)
    assert capsys.readouterr().out == "  fetched 2000...\n"
