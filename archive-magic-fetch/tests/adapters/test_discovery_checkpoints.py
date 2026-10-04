"""Private page/window checkpoints never masquerade as complete listings."""

import json

import pytest

from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex, CATALOG_URL
from archive_magic_fetch.contracts import DiscoveryRequest
from archive_magic_fetch.models import CaptureRef
from archive_magic_fetch.pipeline.discovery.cache import wayback_path
from archive_magic_fetch.pipeline.discovery.load_or_fetch_common_crawl_year import load_or_fetch_common_crawl_year
from archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx import load_or_fetch_year_cdx, _CdxResult
from common_crawl_helpers import Client, Response, collection, record, row
from helpers import make_capt


@pytest.mark.parametrize("change", ["none", "page_count", "catalog"])
def test_common_crawl_resumes_completed_empty_pages_and_invalidates_changed_unit(tmp_path, change):
    capture = record()[0]
    failed, changed = [True], [False]
    calls = []
    def serve(url, **kw):
        if url == CATALOG_URL:
            catalog = collection()
            if changed[0] and change == "catalog":
                catalog = {**catalog, "to": "2017-08-01T00:00:00"}
            return Response(json.dumps([catalog]))
        params = kw["params"]
        if "showNumPages" in params:
            return Response(json.dumps({"pages": 4 if changed[0] and change == "page_count" else 3}))
        page = params["page"]
        calls.append(page)
        if page == 2 and failed[0]:
            raise KeyboardInterrupt()
        if page == 1:
            return Response('{"message":"No Captures found for: example.org"}', status=404)
        return Response(json.dumps(row(capture)))
    def acquire():
        return load_or_fetch_common_crawl_year(
            DiscoveryRequest("*.example.org", 2017, 2017), index_directory=tmp_path / "discovery",
            index=CommonCrawlIndex(sleep=lambda _: None, clock=lambda: 0), client=Client(serve),
        )
    with pytest.raises(KeyboardInterrupt):
        acquire()
    assert calls == [0, 1, 2]
    assert not list((tmp_path / "discovery").rglob("2017.json"))
    empty_page = next((tmp_path / ".state" / "discovery").rglob("1.json"))
    assert json.loads(empty_page.read_text())["captures"] == []
    failed[0], changed[0] = False, True
    calls.clear()
    result = acquire()
    assert result.captures
    expected = [2] if change == "none" else list(range(4 if change == "page_count" else 3))
    assert calls == expected
    assert not (tmp_path / ".state" / "discovery").exists()


@pytest.mark.parametrize("current", [False, True])
def test_wayback_completed_empty_windows_resume_only_for_historical_years(tmp_path, monkeypatch, current):
    import archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx as cdx
    calls, broken = [], [True]
    year = ("20040101000000", "20041231235959")
    first = ("20040101000000", "20040128235959")
    second = ("20040129000000", "20040225235959")
    capture = CaptureRef(make_capt(ts="20040201000000"), "text/html")
    def fetch(**kw):
        scope = kw["date_start"], kw["date_end"]
        calls.append(scope)
        if scope == year:
            raise TimeoutError("CDX wall-clock budget exceeded")
        if scope == second and broken[0]:
            raise KeyboardInterrupt()
        captures = (capture,) if scope[0] <= capture.identity.timestamp <= scope[1] else ()
        return _CdxResult(captures, "example.org", "domain")
    monkeypatch.setattr(cdx, "_fetch_cdx", fetch)
    def acquire():
        return load_or_fetch_year_cdx(index_directory=tmp_path / "discovery", year=2004,
                                     current_year=2004 if current else 2005, url_pattern="*.example.org")
    with pytest.raises(KeyboardInterrupt):
        acquire()
    assert calls == [year, first, second]
    assert (tmp_path / ".state" / "discovery").exists() is (not current)
    broken[0] = False
    calls.clear()
    assert acquire().captures == (capture,)
    if current:
        assert calls[:3] == [year, first, second]
        assert not (tmp_path / "discovery").exists()
    else:
        assert calls[0] == second and year not in calls and first not in calls
        assert wayback_path(tmp_path / "discovery", "*.example.org", 2004).exists()
    assert not (tmp_path / ".state" / "discovery").exists()


def test_invalid_wayback_window_is_preserved_without_network_retry(tmp_path, monkeypatch):
    import archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx as cdx
    broken = [True]
    def fetch(**kw):
        if kw["date_end"] == "20041231235959" and kw["date_start"] == "20040101000000":
            raise TimeoutError("CDX wall-clock budget exceeded")
        if kw["date_start"] != "20040101000000" and broken[0]:
            raise KeyboardInterrupt()
        return _CdxResult((), "example.org", "domain")
    monkeypatch.setattr(cdx, "_fetch_cdx", fetch)
    def acquire():
        return load_or_fetch_year_cdx(index_directory=tmp_path / "discovery", year=2004,
                                     current_year=2005, url_pattern="*.example.org")
    with pytest.raises(KeyboardInterrupt):
        acquire()
    path = next(p for p in (tmp_path / ".state").rglob("*.json")
                if json.loads(p.read_text()).get("captures") == [])
    value = json.loads(path.read_text())
    value["query"] = {"url": "wrong.org"}
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **kw: pytest.fail("invalid checkpoint queried again"))
    with pytest.raises(ValueError, match="invalid CDX checkpoint"):
        acquire()
    assert path.read_bytes() == before
