from datetime import datetime, timezone

import pytest
from wayback import CdxRecord

from archive_magic_fetch import fetch
from archive_magic_fetch.collection_coverage import (
    CollectionCoverage,
    CoverageModeError,
)
from archive_magic_fetch.warc_files import BuiltFiles, WarcCounts


def capture(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    statuscode=200,
    captured="20000101000000",
    digest="A" * 32,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=datetime.strptime(captured, "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        ),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest=digest,
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
        "fresh": False,
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
            "archive_root": tmp_path / "archive",
            "sources_root": tmp_path / "sources",
            "coverage_path": tmp_path / "collection.json",
            "replay_index": tmp_path / "replay" / "index.cdxj",
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
    monkeypatch.setattr(fetch, "list_collection_warcs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(fetch, "save_coverage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        fetch,
        "resolve_prior_coverage",
        lambda *_args, **_kwargs: None,
    )
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
    from archive_magic_fetch.redirects import (
        RedirectExpansion,
        RedirectSearch,
        RedirectScope,
    )

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


def test_run_fetch_merges_prior_coverage_into_search_window(
    monkeypatch,
    tmp_path,
    capsys,
):
    early = capture(captured="19990101000000")
    late = capture(captured="20080101000000", digest="B" * 32)
    searches = []
    _client, paths = install_common(monkeypatch, tmp_path, [early, late])
    prior = CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    monkeypatch.setattr(
        fetch,
        "resolve_prior_coverage",
        lambda *_args, **_kwargs: prior,
    )

    def search(_client, pattern, start, end, **kwargs):
        searches.append((pattern, start, end))
        return [early, late]

    monkeypatch.setattr(fetch, "search_captures", search)
    monkeypatch.setattr(
        fetch,
        "build_warc_files",
        lambda *_args, **_kwargs: BuiltFiles(
            WarcCounts(selected=2, responses=2),
            (),
        ),
    )
    saved = []

    def save(layout, coverage_value):
        saved.append((layout, coverage_value))
        return layout.coverage_path

    monkeypatch.setattr(fetch, "save_coverage", save)

    assert fetch.run_fetch(
        settings(date_start="2005", date_end="2010")
    ) is True
    assert searches == [("example.com/*", "1995", "2010")]
    assert saved[0][0] is paths
    assert saved[0][1].date_start == "1995"
    assert saved[0][1].date_end == "2010"
    assert "Merge: expanding search 2005-2010" in capsys.readouterr().out


def test_run_fetch_fresh_skips_prior_coverage(monkeypatch, tmp_path, capsys):
    late = capture(captured="20080101000000")
    searches = []
    install_common(monkeypatch, tmp_path, [late])
    prior = CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    monkeypatch.setattr(
        fetch,
        "resolve_prior_coverage",
        lambda *_args, **_kwargs: prior,
    )

    def search(_client, pattern, start, end, **kwargs):
        searches.append((start, end))
        return [late]

    monkeypatch.setattr(fetch, "search_captures", search)
    monkeypatch.setattr(
        fetch,
        "build_warc_files",
        lambda *_args, **_kwargs: BuiltFiles(
            WarcCounts(selected=1, responses=1),
            (),
        ),
    )

    assert fetch.run_fetch(
        settings(date_start="2005", date_end="2010", fresh=True)
    ) is True
    assert searches == [("2005", "2010")]
    assert "Merge:" not in capsys.readouterr().out


def test_run_fetch_mode_mismatch_raises(monkeypatch, tmp_path):
    install_common(monkeypatch, tmp_path, [])
    prior = CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    monkeypatch.setattr(
        fetch,
        "resolve_prior_coverage",
        lambda *_args, **_kwargs: prior,
    )
    with pytest.raises(CoverageModeError, match="warc_mode"):
        fetch.run_fetch(settings(warc_mode="latest"))


def test_finalize_indexes_all_collection_warcs(monkeypatch, tmp_path):
    paths = type(
        "Paths",
        (),
        {
            "collection_root": tmp_path,
            "website_root": tmp_path / "website",
            "archive_root": tmp_path / "archive",
            "replay_index": tmp_path / "replay" / "index.cdxj",
        },
    )()
    left = tmp_path / "archive" / "example.com" / "old.warc.gz"
    right = tmp_path / "archive" / "example.com" / "new.warc.gz"
    left.parent.mkdir(parents=True)
    left.write_bytes(b"old")
    right.write_bytes(b"new")
    indexed = []

    def build_index(warcs, *, layout):
        indexed.append(list(warcs))
        return layout.replay_index

    monkeypatch.setattr(fetch, "build_replay_index", build_index)
    monkeypatch.setattr(
        fetch,
        "list_collection_warcs",
        lambda layout: [left, right],
    )
    result = BuiltFiles(
        WarcCounts(selected=1, responses=1),
        (right,),
    )
    assert fetch._finalize_outputs(settings(), result, paths) is True
    assert indexed == [[left, right]]
