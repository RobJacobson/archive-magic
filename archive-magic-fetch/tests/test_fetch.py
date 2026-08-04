from datetime import datetime, timezone

from wayback import CdxRecord

from archive_magic_fetch import fetch
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


def test_run_fetch_passes_primary_histories_and_inline_redirect_expand(
    monkeypatch,
    tmp_path,
):
    primary = capture(statuscode=301)
    client, paths = install_common(monkeypatch, tmp_path, [primary])
    calls = []

    def build(groups, active_client, **kwargs):
        calls.append((groups, active_client, kwargs))
        return BuiltFiles(WarcCounts(selected=1, responses=1), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)
    monkeypatch.setattr(
        fetch,
        "allocate_warc_paths",
        lambda captures_by_url, layout: {
            layout.collection_root / "archive" / "example.com" / "index.warc.gz": (
                ("example.com", primary.urlkey),
            )
        },
    )

    assert fetch.run_fetch(settings(redirect_capture="website")) is True
    assert len(calls) == 1
    assert calls[0][1] is client
    assert set(calls[0][0]) == {("example.com", primary.urlkey)}
    assert calls[0][2]["layout"] is paths
    assert calls[0][2]["worker_count"] == 8
    assert calls[0][2]["collect_redirects"] is True
    assert callable(calls[0][2]["expand_redirects"])


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
    assert "Redirects:" not in output


def test_redirect_capture_disabled_when_warc_none(monkeypatch, tmp_path):
    selected = capture()
    install_common(monkeypatch, tmp_path, [selected])
    calls = []

    def build(groups, active_client, **kwargs):
        calls.append(kwargs)
        return BuiltFiles(WarcCounts(), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)
    monkeypatch.setattr(
        fetch,
        "prepare_website_files",
        lambda *_args, **_kwargs: object(),
    )

    assert fetch.run_fetch(
        settings(warc_mode="none", files_mode="latest", redirect_capture="page")
    ) is True
    assert calls[0]["collect_redirects"] is False
    assert calls[0]["expand_redirects"] is None


def test_files_mode_still_prepares_website_files_without_redirect_stage(
    monkeypatch,
    tmp_path,
):
    primary = capture(statuscode=301)
    client, _paths = install_common(monkeypatch, tmp_path, [primary])
    website_files = object()
    monkeypatch.setattr(
        fetch,
        "prepare_website_files",
        lambda *_args, **_kwargs: website_files,
    )
    monkeypatch.setattr(
        fetch,
        "allocate_warc_paths",
        lambda captures_by_url, layout: {
            layout.collection_root / "archive" / "example.com" / "index.warc.gz": (
                ("example.com", primary.urlkey),
            )
        },
    )
    builds = []

    def build(captures_by_url, active_client, **kwargs):
        assert active_client is client
        builds.append((captures_by_url, kwargs))
        return BuiltFiles(WarcCounts(selected=1, responses=1), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)

    assert fetch.run_fetch(
        settings(
            warc_mode="latest",
            files_mode="latest",
            redirect_capture="page",
        )
    ) is True

    captures_by_url, kwargs = builds[0]
    assert set(captures_by_url) == {("example.com", primary.urlkey)}
    assert set(kwargs["file_captures_by_url"]) == {
        ("example.com", primary.urlkey)
    }
    assert kwargs["website_files"] is website_files
    assert kwargs["collect_redirects"] is True
    assert callable(kwargs["expand_redirects"])


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


def test_redirect_expand_marks_same_site_and_foreign_batches(monkeypatch, tmp_path):
    from archive_magic_fetch.redirects import RedirectExpansion, RedirectSearch, RedirectScope

    subdomain = capture(
        urlkey="org,seed)/news",
        original="https://news.seed.org/",
        statuscode=200,
    )
    foreign = capture(
        urlkey="com,other)/",
        original="https://other.com/",
        statuscode=200,
    )

    layout = type(
        "Paths",
        (),
        {
            "collection_root": tmp_path,
            "archive_root": tmp_path / "archive",
        },
    )()
    known: set = set()
    reserved: set = set()
    seen: set = set()

    def fake_expand_target(client, target, **kwargs):
        if target == "https://news.seed.org/":
            histories = {("news.seed.org", subdomain.urlkey): [subdomain]}
            return RedirectExpansion(
                RedirectSearch(
                    RedirectScope(
                        ("news.seed.org", None, "/", ""),
                        target,
                        "exact",
                    ),
                    (subdomain,),
                ),
                histories,
            )
        if target == "https://other.com/":
            histories = {("other.com", foreign.urlkey): [foreign]}
            return RedirectExpansion(
                RedirectSearch(
                    RedirectScope(("other.com", None, "/", ""), target, "exact"),
                    (foreign,),
                ),
                histories,
            )
        return None

    monkeypatch.setattr(fetch, "expand_redirect_target", fake_expand_target)
    monkeypatch.setattr(
        fetch,
        "save_search_results",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        fetch,
        "allocate_warc_paths",
        lambda histories, layout: {
            layout.archive_root / key[0] / "index.warc.gz": (key,)
            for key in histories
        },
    )

    expand = fetch._redirect_expand(
        client=object(),
        seed_pattern="seed.org/*",
        mode="page",
        date_start="1995",
        date_end="2020",
        layout=layout,
        known_history_keys=known,
        reserved_paths=reserved,
        seen_searches=seen,
        retries=0,
    )
    batches = expand(
        ["https://news.seed.org/", "https://other.com/"]
    )
    by_domain = {batch.histories[0].domain: batch.expand for batch in batches}
    assert by_domain == {"news.seed.org": True, "other.com": False}
