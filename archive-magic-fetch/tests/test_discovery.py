from datetime import datetime, timezone

import pytest
from wayback import CdxRecord
from wayback.exceptions import RateLimitError, UnexpectedResponseFormat

from archive_magic_fetch import cli, discovery, retry
from archive_magic_fetch.retrieval import DEFAULT_CONCURRENCY


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def install_job_clock(monkeypatch, *, elapsed_seconds=0):
    started = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
    ended = datetime.fromtimestamp(
        started.timestamp() + elapsed_seconds,
        tz=timezone.utc,
    )
    wall_times = iter((started, ended))
    ticks = iter((100.0, 100.0 + elapsed_seconds))
    monkeypatch.setattr(cli, "_utc_now", lambda: next(wall_times))
    monkeypatch.setattr(cli, "_monotonic", lambda: next(ticks))


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


def test_select_latest_uses_newest_redirect_for_redirect_only_groups():
    only_301 = record(captured="20200101000000", statuscode=301)
    only_302 = record(captured="20210101000000", statuscode=302)

    assert (
        discovery.select_latest_capture([only_301, only_302]) is only_302
    )


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
        third.urlkey: [third],
    }
    assert discovery.apply_output_mode(groups, "all") == {
        first.urlkey: [first, second],
        third.urlkey: [third],
    }
    assert discovery.apply_output_mode(groups, "unique") == {
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
                "limit": 10_000,
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
                "limit": 10_000,
                "resolve_revisits": False,
            },
        )
    ]


def test_discover_reports_progress_after_each_request_limit(monkeypatch):
    monkeypatch.setattr(discovery, "_CDX_REQUEST_LIMIT", 2)
    expected = [record() for _ in range(5)]
    reported = []
    limits = []

    class Client:
        def search(self, *args, **kwargs):
            limits.append(kwargs["limit"])
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
    assert limits == [2]
    assert reported == [2, 4]


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

    monkeypatch.setattr(discovery, "sleep_seconds", sleeps.append)

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
    assert sleeps == [10]
    assert reported == []
    output = capsys.readouterr().out
    assert "https://web.archive.org/cdx/search/cdx?" in output
    assert "retry 1/8 in 10s" in output


def test_discover_rate_limit_without_retry_after_uses_exponential_delay(
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

    monkeypatch.setattr(discovery, "sleep_seconds", sleeps.append)

    assert discovery.discover(Client(), "example.com", "1995", "2020") == []
    assert sleeps == [10]
    assert "retry 1/8 in 10s" in capsys.readouterr().out


def test_discover_retries_repeated_rate_limits(monkeypatch, capsys):
    sleeps = []
    attempts = 0

    class Client:
        def search(self, *args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                raise RateLimitError(None, 3)
            return iter([])

    monkeypatch.setattr(discovery, "sleep_seconds", sleeps.append)

    assert discovery.discover(Client(), "example.com", "1995", "2020") == []
    assert attempts == 4
    assert sleeps == [10, 20, 40]
    assert capsys.readouterr().out.count(" : retry ") == 3


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


def test_group_captures_preserves_value_equal_records():
    capture = record()

    grouped = discovery.group_captures([capture, record()])

    assert grouped[capture.urlkey] == [capture, capture]


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

    def make_client_factory(user_agent):
        session = FakeSession(user_agent)
        created["session"] = session
        client = FakeClient(session)
        created["client"] = client
        return lambda: client

    monkeypatch.setattr(cli, "make_client_factory", make_client_factory)
    return created


def test_cli_owns_one_client_context_and_passes_same_client(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    capture = record()
    groups = {capture.urlkey: [capture]}
    layout = object()
    buckets = (object(),)
    calls = {}

    def fake_discover(
        client,
        pattern,
        start,
        end,
        *,
        progress=None,
        retries=None,
    ):
        calls["discover"] = (
            client,
            pattern,
            start,
            end,
            progress,
            retries,
        )
        return [capture]

    def fake_export(
        grouped,
        planned_buckets,
        client,
        **kwargs,
    ):
        calls["export"] = (
            grouped,
            planned_buckets,
            client,
            kwargs,
        )
        return type(
            "Result",
            (),
            {
                "summary": type("Summary", (), {"selected": 1})(),
                "final_warcs": (),
                "failed_capture_urls": (),
                "files_summary": type(
                    "FilesSummary",
                    (),
                    {"written": 0},
                )(),
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
    assert calls["export"][3]["client_factory"] is not None
    assert calls["export"][3]["concurrency"] == DEFAULT_CONCURRENCY
    assert calls["export"][3]["retries"] == retry.DEFAULT_RETRIES
    assert calls["export"][3]["files_mode"] == "none"
    assert calls["replay"] == ((), layout)
    assert calls["summary"][0].selected == 1
    assert calls["summary"][1] == {"warc_mode": "all"}


def test_cli_prints_stage_messages(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    install_job_clock(monkeypatch, elapsed_seconds=95)
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
                "final_warcs": (),
                "failed_capture_urls": (),
                "files_summary": type(
                    "FilesSummary",
                    (),
                    {"written": 0},
                )(),
            },
        )(),
    )
    monkeypatch.setattr(cli, "generate_replay_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "print_summary", lambda summary, **kwargs: None)

    assert cli.main(["*.example.com"]) == 0
    output = capsys.readouterr().out
    assert output == (
        "Job started: 2026-07-24T12:00:00Z\n"
        "Discovering captures for *.example.com (1995-20260722123456)\n"
        "Discovered 1 captures\n"
        "Saving source acquisition...\n"
        "Grouping 1 captures...\n"
        "Exporting 1 URL groups (concurrency=8)...\n"
        "Building replay index...\n"
        "Job ended: 2026-07-24T12:01:35Z\n"
        "Job duration: 1.6 minutes\n"
    )


def test_cli_finishes_outputs_then_lists_failures_and_returns_one(
    monkeypatch,
    capsys,
):
    install_fake_lifecycle(monkeypatch)
    capture = record()
    groups = {capture.urlkey: [capture]}
    layout = object()
    bucket = object()
    failed_url = capture.view_url
    finalized = []

    monkeypatch.setattr(cli, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(cli, "discover", lambda *args, **kwargs: [capture])
    monkeypatch.setattr(cli, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "group_captures", lambda captures: groups)
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
                "final_warcs": (),
                "failed_capture_urls": (failed_url,),
                "files_summary": type(
                    "FilesSummary",
                    (),
                    {"written": 0},
                )(),
            },
        )(),
    )
    monkeypatch.setattr(
        cli,
        "generate_replay_index",
        lambda *args, **kwargs: finalized.append("index"),
    )
    monkeypatch.setattr(cli, "print_summary", lambda *args, **kwargs: None)

    assert cli.main(["example.com/*"]) == 1
    assert finalized == ["index"]
    output = capsys.readouterr().out
    assert f"Failed captures:\n{failed_url}\n" in output


def test_cli_passes_explicit_dates(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    calls = {}

    def fake_discover(
        client,
        pattern,
        start,
        end,
        *,
        progress=None,
        retries=None,
    ):
        calls["discover"] = (
            client,
            pattern,
            start,
            end,
            progress,
            retries,
        )
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
    install_job_clock(monkeypatch)

    def fake_discover(
        client,
        pattern,
        start,
        end,
        *,
        progress=None,
        retries=None,
    ):
        return []

    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")

    assert cli.main(["example.com/*"]) == 0
    assert capsys.readouterr().out == (
        "Job started: 2026-07-24T12:00:00Z\n"
        "Discovering captures for example.com/* (1995-20260722123456)\n"
        "No captures found\n"
        "Job ended: 2026-07-24T12:00:00Z\n"
        "Job duration: 0.0 minutes\n"
    )


def test_cli_fatal_error_returns_one(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    install_job_clock(monkeypatch, elapsed_seconds=30)

    def fail(*args, **kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(cli, "discover", fail)

    assert cli.main(["example.com/*"]) == 1
    output = capsys.readouterr()
    assert output.err == "ERROR: discovery failed\n"
    assert output.out.endswith(
        "Job ended: 2026-07-24T12:00:30Z\n"
        "Job duration: 0.5 minutes\n"
    )


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
                "final_warcs": (object(),),
                "failed_capture_urls": (),
                "files_summary": type(
                    "FilesSummary",
                    (),
                    {"written": 0},
                )(),
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
                "final_warcs": (),
                "failed_capture_urls": (),
                "files_summary": type(
                    "FilesSummary",
                    (),
                    {"written": 0},
                )(),
            },
        )(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("downstream failed")

    monkeypatch.setattr(cli, "generate_replay_index", fail)

    assert cli.main(["https://example.com/*"]) == 1
    acquisitions = list(
        (tmp_path / "archives" / "example.com" / "sources").iterdir()
    )
    assert len(acquisitions) == 1
    assert (acquisitions[0] / "captures.cdx.gz").exists()
    assert (acquisitions[0] / "query.json").exists()
    log = (acquisitions[0] / "log.txt").read_text()
    assert "Job started:" in log
    assert "Saving source acquisition..." in log
    assert "ERROR: downstream failed" in log
    assert "Job ended:" in log


def test_cli_defaults_parse_to_warc_all_and_files_none():
    args = cli.parse_args(["example.com/*"])
    assert args.warc == "all"
    assert args.files == "none"
    assert args.rewrite_local is False
    assert args.concurrency == DEFAULT_CONCURRENCY
    assert args.retries == retry.DEFAULT_RETRIES


def test_cli_accepts_unique_only_for_files():
    assert cli.parse_args(
        ["example.com/*", "--files", "unique"]
    ).files == "unique"
    with pytest.raises(SystemExit):
        cli.parse_args(["example.com/*", "--warc", "unique"])


def test_cli_concurrency_one_is_serial_diagnostic_mode():
    args = cli.parse_args(["example.com/*", "--concurrency", "1"])
    assert args.concurrency == 1


def test_cli_accepts_zero_and_arbitrary_retry_counts():
    assert cli.parse_args(
        ["example.com/*", "--retries", "0"]
    ).retries == 0
    assert cli.parse_args(
        ["example.com/*", "--retries", "100"]
    ).retries == 100


def test_cli_rejects_negative_retries(capsys):
    assert cli.main(["example.com/*", "--retries", "-1"]) == 2
    assert "--retries cannot be negative" in capsys.readouterr().err


def test_cli_rejects_non_integer_retries():
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["example.com/*", "--retries", "1.5"])

    assert raised.value.code == 2


def test_cli_rejects_concurrency_below_one(capsys):
    assert cli.main(["example.com/*", "--concurrency", "0"]) == 2
    assert "--concurrency must be at least 1" in capsys.readouterr().err


def test_cli_rewrite_local_requires_files_mode(capsys):
    assert cli.main(
        ["example.com/*", "--rewrite-local", "--files", "none"]
    ) == 2
    assert (
        "--rewrite-local requires --files latest, unique, or all"
        in capsys.readouterr().err
    )


def test_cli_rewrite_local_alone_does_not_enable_files(capsys):
    assert cli.main(["example.com/*", "--rewrite-local", "--warc", "none"]) == 2
    assert (
        "--rewrite-local requires --files latest, unique, or all"
        in capsys.readouterr().err
    )


def test_cli_both_none_is_successful_noop(monkeypatch, capsys):
    install_job_clock(monkeypatch)

    def fail(*args, **kwargs):
        raise AssertionError("network client should not be created")

    monkeypatch.setattr(cli, "make_client_factory", fail)
    monkeypatch.setattr(cli, "collection_layout", fail)

    assert cli.main(["example.com/*", "--warc", "none", "--files", "none"]) == 0
    assert capsys.readouterr().out == (
        "Job started: 2026-07-24T12:00:00Z\n"
        "Nothing to do: both --warc and --files are none\n"
        "Job ended: 2026-07-24T12:00:00Z\n"
        "Job duration: 0.0 minutes\n"
    )


def test_cli_rejects_unapproved_arguments():
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["example.com/*", "--output", "elsewhere"])

    assert error.value.code == 2


def test_report_discovery_progress(capsys):
    cli._report_discovery_progress(2000)
    assert capsys.readouterr().out == "  fetched 2000...\n"
