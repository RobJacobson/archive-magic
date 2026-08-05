from datetime import datetime, timezone
from wayback import CdxRecord

from archive_magic_fetch import fetch
from archive_magic_fetch.collection_coverage import CollectionCoverage
from archive_magic_fetch.redirects import RedirectReport
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
        "build_warc": True,
        "files_mode": "none",
        "rewrite_local": False,
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
    monkeypatch.setattr(fetch, "resolve_prior_coverage", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        fetch,
        "write_redirect_report",
        lambda _warcs, path: RedirectReport(path, 0, 0, 0),
    )
    return client, paths


def test_run_fetch_passes_all_primary_histories_without_redirect_expansion(
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

    assert fetch.run_fetch(settings()) is True
    groups, active_client, kwargs = calls[0]
    assert active_client is client
    assert set(groups) == {("example.com", primary.urlkey)}
    assert kwargs["layout"] is paths
    assert kwargs["worker_count"] == 8
    assert "collect_redirects" not in kwargs
    assert "expand_redirects" not in kwargs


def test_run_fetch_counts_failed_capture_identities(monkeypatch, tmp_path, capsys):
    selected = capture()
    install_common(monkeypatch, tmp_path, [selected])
    monkeypatch.setattr(
        fetch,
        "build_warc_files",
        lambda *_args, **_kwargs: BuiltFiles(
            WarcCounts(selected=2, playback_failures=2),
            (),
            failed_capture_urls=(selected.view_url, selected.view_url),
        ),
    )

    assert fetch.run_fetch(settings()) is False
    output = capsys.readouterr().out
    assert "2 selected, 0 responses, 0 revisits, 2 failed" in output


def test_build_warc_false_keeps_loose_file_pipeline(monkeypatch, tmp_path):
    selected = capture()
    install_common(monkeypatch, tmp_path, [selected])
    website_files = object()
    monkeypatch.setattr(
        fetch,
        "prepare_website_files",
        lambda *_args, **_kwargs: website_files,
    )
    calls = []

    def build(groups, _client, **kwargs):
        calls.append((groups, kwargs))
        return BuiltFiles(WarcCounts(), ())

    monkeypatch.setattr(fetch, "build_warc_files", build)
    assert fetch.run_fetch(
        settings(build_warc=False, files_mode="latest")
    ) is True
    groups, kwargs = calls[0]
    assert groups == {}
    assert kwargs["website_files"] is website_files
    assert kwargs["file_captures_by_url"]


def test_empty_search_is_compact_success(monkeypatch, tmp_path, capsys):
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
    assert fetch.run_fetch(
        settings(build_warc=False, files_mode="none")
    ) is True
    assert "Nothing to do" in capsys.readouterr().out


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
        files_mode="none",
    )
    monkeypatch.setattr(fetch, "resolve_prior_coverage", lambda *_a, **_k: prior)

    def search(_client, pattern, start, end, **_kwargs):
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
    monkeypatch.setattr(
        fetch,
        "save_coverage",
        lambda layout, value: saved.append((layout, value)),
    )

    assert fetch.run_fetch(settings(date_start="2005", date_end="2010")) is True
    assert searches == [("example.com/*", "1995", "2010")]
    assert saved[0][0] is paths
    assert saved[0][1].date_start == "1995"
    assert saved[0][1].date_end == "2010"
    assert "Merge: expanding search 2005-2010" in capsys.readouterr().out


def test_finalize_indexes_and_reports_all_collection_warcs(monkeypatch, tmp_path):
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
    indexed = []
    reported = []
    monkeypatch.setattr(fetch, "list_collection_warcs", lambda _layout: [left, right])
    monkeypatch.setattr(
        fetch,
        "build_replay_index",
        lambda warcs, *, layout: indexed.append(list(warcs)) or layout.replay_index,
    )
    monkeypatch.setattr(
        fetch,
        "write_redirect_report",
        lambda warcs, path: reported.append((list(warcs), path))
        or RedirectReport(path, 1, 2, 3),
    )
    result = BuiltFiles(WarcCounts(selected=1, responses=1), (right,))
    source = tmp_path / "sources" / "run"

    assert fetch._finalize_outputs(settings(), result, paths, source) is True
    assert indexed == [[left, right]]
    assert reported == [([left, right], source / "redirects.json")]
