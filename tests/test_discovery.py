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
            "example.com/*",
            {
                "from_date": "1995",
                "to_date": "2020",
                "resolve_revisits": False,
            },
        )
    ]


def test_discover_discards_partial_attempt_and_rematerializes_after_rate_limit(
    monkeypatch,
):
    first = record(captured="20000101000000")
    second = record(captured="20010101000000")
    attempts = 0
    sleeps = []

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

    assert discovery.discover(Client(), "example.com", "1995", "2020") == [
        first,
        second,
    ]
    assert attempts == 2
    assert sleeps == [7]


def test_discover_rate_limit_without_retry_after_uses_sixty_seconds(
    monkeypatch,
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
    output_paths = {capture.urlkey: object()}
    calls = {}

    def fake_discover(client, pattern, start, end):
        calls["discover"] = (client, pattern, start, end)
        return [capture]

    def fake_export(grouped, paths, client):
        calls["export"] = (grouped, paths, client)

    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")
    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(cli, "group_captures", lambda captures: groups)
    monkeypatch.setattr(cli, "preflight_paths", lambda grouped: output_paths)
    monkeypatch.setattr(cli, "export_all", fake_export)

    assert cli.main(["*.example.com"]) == 0
    client = created["client"]
    assert created["session"].user_agent == cli.USER_AGENT
    assert client.session is created["session"]
    assert client.entered is True
    assert client.exited is True
    assert calls["discover"] == (
        client,
        "*.example.com",
        "1995",
        "20260722123456",
    )
    assert calls["export"] == (groups, output_paths, client)


def test_cli_passes_explicit_dates(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    calls = {}

    def fake_discover(client, pattern, start, end):
        calls["discover"] = (client, pattern, start, end)
        return []

    monkeypatch.setattr(cli, "discover", fake_discover)

    assert cli.main(
        ["example.com/*", "--start", "2018", "--end", "20200131"]
    ) == 0
    assert calls["discover"] == (
        created["client"],
        "example.com/*",
        "2018",
        "20200131",
    )


def test_cli_empty_result_is_success(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    monkeypatch.setattr(cli, "discover", lambda *args: [])

    assert cli.main(["example.com/*"]) == 0
    assert capsys.readouterr().out == "No captures found\n"


def test_cli_fatal_error_returns_one(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)

    def fail(*args):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(cli, "discover", fail)

    assert cli.main(["example.com/*"]) == 1
    assert capsys.readouterr().err == "ERROR: discovery failed\n"


def test_cli_rejects_unapproved_arguments():
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["example.com/*", "--output", "elsewhere"])

    assert error.value.code == 2
