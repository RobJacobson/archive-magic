from datetime import datetime, timezone
from pathlib import Path

from wayback import CdxRecord

from archive_magic_fetch import fetch
from archive_magic_fetch.redirects import RedirectDiscovery, RedirectSearch
from archive_magic_fetch.warc_files import BuiltFiles, WarcCounts


def capture(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    statuscode=200,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest="A" * 32,
        length=100,
    )


def settings(**overrides):
    values = {
        "url_pattern": "example.com/*",
        "date_start": "1995",
        "date_end": "20260803000000",
        "warc_mode": "all",
        "files_mode": "none",
        "rewrite_local": False,
        "redirect_capture": "none",
        "worker_count": 8,
        "retries": 0,
    }
    values.update(overrides)
    return fetch.FetchSettings(**values)


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def install_common(monkeypatch, tmp_path, captures):
    client = Client()
    paths = type(
        "Paths",
        (),
        {
            "collection_root": tmp_path,
            "website_root": tmp_path / "website",
        },
    )()
    monkeypatch.setattr(fetch, "make_client_factory", lambda _agent: lambda: client)
    monkeypatch.setattr(fetch, "collection_paths", lambda *_args, **_kwargs: paths)
    monkeypatch.setattr(fetch, "search_captures", lambda *_args, **_kwargs: captures)
    monkeypatch.setattr(
        fetch,
        "save_search_results",
        lambda *_args, **_kwargs: type("SearchFiles", (), {"path": tmp_path})(),
    )
    monkeypatch.setattr(fetch, "build_replay_index", lambda *_args, **_kwargs: None)
    return client, paths


def test_run_fetch_builds_final_warcs_once_after_redirect_discovery(
    monkeypatch,
    tmp_path,
):
    primary = capture(statuscode=301)
    target = capture(
        urlkey="org,target)/",
        original="https://target.org/",
    )
    client, paths = install_common(monkeypatch, tmp_path, [primary])
    calls = []
    saved_searches = []
    monkeypatch.setattr(
        fetch,
        "save_search_results",
        lambda captures, **kwargs: (
            saved_searches.append((tuple(captures), kwargs["url_pattern"]))
            or type("SearchFiles", (), {"path": tmp_path})()
        ),
    )
    redirects = RedirectDiscovery(
        captures=(target,),
        searches=(
            RedirectSearch(
                type(
                    "Scope",
                    (),
                    {
                        "url": "https://target.org/",
                        "key": ("target.org",),
                        "match_type": "host",
                    },
                )(),
                (target,),
            ),
        ),
        failed_capture_urls=(),
        messages=(),
        additional_domains=1,
    )
    monkeypatch.setattr(
        fetch,
        "discover_redirect_captures",
        lambda *args, **kwargs: redirects,
    )

    def build(groups, active_client, **kwargs):
        calls.append((groups, active_client, kwargs))
        return BuiltFiles(WarcCounts(selected=2, responses=2), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)

    assert fetch.run_fetch(settings(redirect_capture="website")) is True
    assert len(calls) == 1
    assert calls[0][1] is client
    assert set(calls[0][0]) == {
        ("example.com", primary.urlkey),
        ("target.org", target.urlkey),
    }
    assert calls[0][2]["layout"] is paths
    assert calls[0][2]["worker_count"] == 8
    assert saved_searches == [
        ((primary,), "example.com/*"),
        ((target,), "https://target.org/"),
    ]


def test_run_fetch_prints_compact_phases_without_final_failed_url_list(
    monkeypatch,
    tmp_path,
    capsys,
):
    selected = capture()
    install_common(monkeypatch, tmp_path, [selected])
    monkeypatch.setattr(
        fetch,
        "build_warc_files",
        lambda *_args, **_kwargs: BuiltFiles(
            WarcCounts(selected=1, playback_failures=1),
            (),
            failed_capture_urls=(selected.view_url,),
        ),
    )

    assert fetch.run_fetch(settings()) is False
    output = capsys.readouterr().out
    assert "Fetch example.com/* (1995-20260803000000)" in output
    assert "Search: 1 captures in 1 URL histories" in output
    assert "Done in " in output
    assert "1 selected, 0 responses, 0 revisits, 1 failed" in output
    assert "Failed captures:" not in output


def test_redirect_histories_ignore_primary_latest_and_stay_out_of_files(
    monkeypatch,
    tmp_path,
):
    primary = capture(statuscode=301)
    first_target = capture(
        urlkey="org,target)/",
        original="https://target.org/",
    )
    second_target = CdxRecord(
        urlkey=first_target.urlkey,
        timestamp=datetime(2001, 1, 1, tzinfo=timezone.utc),
        original=first_target.original,
        mimetype=first_target.mimetype,
        statuscode=first_target.statuscode,
        digest="B" * 32,
        length=first_target.length,
    )
    client, _paths = install_common(monkeypatch, tmp_path, [primary])
    website_files = object()
    monkeypatch.setattr(
        fetch,
        "prepare_website_files",
        lambda *_args, **_kwargs: website_files,
    )
    monkeypatch.setattr(
        fetch,
        "discover_redirect_captures",
        lambda *_args, **_kwargs: RedirectDiscovery(
            captures=(first_target, second_target),
            searches=(),
            failed_capture_urls=(),
            messages=(),
            additional_domains=1,
        ),
    )
    builds = []

    def build(captures_by_url, active_client, **kwargs):
        assert active_client is client
        builds.append((captures_by_url, kwargs))
        return BuiltFiles(WarcCounts(selected=3, responses=3), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)

    assert fetch.run_fetch(
        settings(
            warc_mode="latest",
            files_mode="latest",
            redirect_capture="page",
        )
    ) is True

    captures_by_url, kwargs = builds[0]
    assert captures_by_url[("target.org", "org,target)/")] == [
        first_target,
        second_target,
    ]
    assert set(kwargs["file_captures_by_url"]) == {
        ("example.com", primary.urlkey)
    }
    assert kwargs["website_files"] is website_files


def test_run_fetch_empty_search_is_compact_success(monkeypatch, tmp_path, capsys):
    install_common(monkeypatch, tmp_path, [])

    assert fetch.run_fetch(settings()) is True
    output = capsys.readouterr().out
    assert "Search: 0 captures in 0 URL histories" in output
    assert output.rstrip().endswith("Done in 0.0 minutes")


def test_both_outputs_disabled_avoids_client_creation(monkeypatch, capsys):
    monkeypatch.setattr(
        fetch,
        "make_client_factory",
        lambda *_args: (_ for _ in ()).throw(AssertionError("network")),
    )

    assert fetch.run_fetch(settings(warc_mode="none", files_mode="none")) is True
    assert "Nothing to do" in capsys.readouterr().out
