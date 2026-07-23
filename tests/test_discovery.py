from collections import OrderedDict

import pytest

from archive_magic_fetch import cli, discovery


def test_discover_uses_ia_explicit_bounds_and_no_limit(monkeypatch):
    calls = {}
    expected = [
        {
            "urlkey": "com,example)/",
            "url": "https://example.com/",
            "timestamp": "20000101000000",
        }
    ]

    class FakeFetcher:
        def __init__(self, **kwargs):
            calls["init"] = kwargs

        def iter(self, url_pattern, **kwargs):
            calls["iter"] = (url_pattern, kwargs)
            return iter(expected)

    monkeypatch.setattr(discovery.cdx_toolkit, "CDXFetcher", FakeFetcher)

    assert discovery.discover("example.com/*", "1995", "2020") == expected
    assert calls == {
        "init": {"source": "ia"},
        "iter": ("example.com/*", {"from_ts": "1995", "to": "2020"}),
    }


def test_group_captures_uses_urlkey_and_sorts_variants_by_timestamp():
    captures = [
        {
            "urlkey": "com,example)/index.html",
            "url": "https://example.com/index.html",
            "timestamp": "20200102000000",
        },
        {
            "urlkey": "com,example)/index.html",
            "url": "http://www.example.com:80/index.html",
            "timestamp": "20190101000000",
        },
        {
            "urlkey": "com,example)/other.html",
            "url": "https://example.com/other.html",
            "timestamp": "20180101000000",
        },
    ]

    grouped = discovery.group_captures(captures)

    assert list(grouped) == [
        "com,example)/index.html",
        "com,example)/other.html",
    ]
    assert [
        capture["timestamp"]
        for capture in grouped["com,example)/index.html"]
    ] == [
        "20190101000000",
        "20200102000000",
    ]


def test_group_captures_strips_fragments_before_export():
    capture = {
        "urlkey": "com,example)/index.html",
        "url": "http://www.example.com:80/index.html#content-primary",
        "timestamp": "20060114082621",
    }

    grouped = discovery.group_captures([capture])

    assert capture["url"] == "http://www.example.com:80/index.html"
    assert grouped["com,example)/index.html"] == [capture]


def test_group_captures_strips_bare_empty_query_before_export():
    capture = {
        "urlkey": "com,example)/index.html",
        "url": "http://www.example.com:80/index.html?#content-primary",
        "timestamp": "20070129100228",
    }

    grouped = discovery.group_captures([capture])

    assert capture["url"] == "http://www.example.com:80/index.html"
    assert grouped["com,example)/index.html"] == [capture]


def test_group_captures_preserves_nonempty_query():
    capture = {
        "urlkey": "com,example)/index.html?mode=print",
        "url": "https://example.com/index.html?mode=print#content",
        "timestamp": "20200101000000",
    }

    discovery.group_captures([capture])

    assert capture["url"] == "https://example.com/index.html?mode=print"


def test_group_captures_collapses_literal_duplicate_cdx_rows():
    row = {
        "urlkey": "com,example)/index.html",
        "url": "https://example.com/index.html",
        "timestamp": "20200101000000",
        "status": "200",
        "digest": "A" * 32,
    }

    grouped = discovery.group_captures([row.copy(), row.copy()])

    assert grouped["com,example)/index.html"] == [row]


def test_cli_applies_defaults_and_passes_pattern_unchanged(monkeypatch):
    calls = {}
    captures = [
        {
            "urlkey": "com,example)/",
            "url": "https://example.com/",
            "timestamp": "20000101000000",
        }
    ]
    grouped = OrderedDict([("com,example)/", captures)])
    paths = {"com,example)/": object()}

    def fake_discover(pattern, start, end):
        calls["discover"] = (pattern, start, end)
        return captures

    monkeypatch.setattr(cli, "current_utc_cdx_timestamp", lambda: "20260722123456")
    monkeypatch.setattr(cli, "discover", fake_discover)
    monkeypatch.setattr(cli, "group_captures", lambda value: grouped)
    monkeypatch.setattr(cli, "preflight_paths", lambda value: paths)
    monkeypatch.setattr(
        cli, "export_all", lambda groups, output: calls.setdefault("export", (groups, output))
    )

    assert cli.main(["*.example.com"]) == 0
    assert calls["discover"] == ("*.example.com", "1995", "20260722123456")
    assert calls["export"] == (grouped, paths)


def test_cli_passes_explicit_dates(monkeypatch):
    calls = {}

    def fake_discover(pattern, start, end):
        calls["discover"] = (pattern, start, end)
        return []

    monkeypatch.setattr(cli, "discover", fake_discover)

    assert cli.main(["example.com/*", "--start", "2018", "--end", "20200131"]) == 0
    assert calls["discover"] == ("example.com/*", "2018", "20200131")


def test_cli_empty_result_is_success(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover", lambda *args: [])

    assert cli.main(["example.com/*"]) == 0
    assert capsys.readouterr().out == "No captures found\n"


def test_cli_fatal_error_returns_one(monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(cli, "discover", fail)

    assert cli.main(["example.com/*"]) == 1
    assert capsys.readouterr().err == "ERROR: discovery failed\n"


def test_cli_rejects_unapproved_arguments():
    with pytest.raises(SystemExit) as error:
        cli.parse_args(["example.com/*", "--output", "elsewhere"])

    assert error.value.code == 2
