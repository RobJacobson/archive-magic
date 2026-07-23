from collections import OrderedDict

import pytest

from archive_magic_fetch import cli, discovery


def test_discover_uses_ia_explicit_bounds_and_no_limit(monkeypatch):
    calls = {}
    expected = [{"url": "https://example.com/", "timestamp": "20000101000000"}]

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


def test_group_captures_uses_exact_url_and_sorts_by_timestamp():
    captures = [
        {"url": "https://example.com/?a=1", "timestamp": "20200102000000"},
        {"url": "http://example.com/?a=1", "timestamp": "20200101000000"},
        {"url": "https://example.com/?a=1", "timestamp": "20190101000000"},
        {"url": "https://example.com/?a=2", "timestamp": "20180101000000"},
    ]

    grouped = discovery.group_captures(captures)

    assert list(grouped) == [
        "https://example.com/?a=1",
        "http://example.com/?a=1",
        "https://example.com/?a=2",
    ]
    assert [capture["timestamp"] for capture in grouped["https://example.com/?a=1"]] == [
        "20190101000000",
        "20200102000000",
    ]


def test_cli_applies_defaults_and_passes_pattern_unchanged(monkeypatch):
    calls = {}
    captures = [{"url": "https://example.com/", "timestamp": "20000101000000"}]
    grouped = OrderedDict([("https://example.com/", captures)])
    paths = {"https://example.com/": object()}

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

