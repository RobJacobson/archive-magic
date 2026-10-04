"""Exercise the real CC adapter through annual publication-ready local output."""

import json
from dataclasses import replace

from warcio.archiveiterator import ArchiveIterator

from archive_magic_fetch.adapters import build_common_crawl_source as adapter
from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex, CATALOG_URL
from archive_magic_fetch.config.build_settings import FetchSettings
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.pipeline.run_fetch import run_fetch
from common_crawl_helpers import Client, Response, collection, record, row, range_response


def setup_source(tmp_path, monkeypatch, captures_and_bytes):
    sessions, downloads, responses = [], [], []
    by_filename = {capture.locator.filename: (capture, data) for capture, data in captures_and_bytes}
    def serve(url, **kw):
        if url == CATALOG_URL:
            response = Response(json.dumps([collection(start="2017-01-01T00:00:00", end="2018-12-31T23:59:59")]))
        elif url.startswith("https://index.commoncrawl.org/"):
            params = kw["params"]
            if "showNumPages" in params:
                response = Response('{"pages":1}')
            else:
                rows = [row(capture) for capture, _ in captures_and_bytes if params["from"] <= capture.identity.timestamp <= params["to"]]
                response = Response("\n".join(map(json.dumps, rows)))
        else:
            filename = url.removeprefix("https://data.commoncrawl.org/")
            capture, data = by_filename[filename]
            downloads.append(capture)
            response = range_response(data, capture)
        responses.append(response)
        return response
    def session():
        client = Client(serve)
        sessions.append(client)
        return client
    monkeypatch.setattr(adapter.requests, "Session", session)
    monkeypatch.setattr(adapter, "_RangeSession", session)
    monkeypatch.setattr(adapter, "CommonCrawlIndex", lambda: CommonCrawlIndex(sleep=lambda _: None))
    settings = FetchSettings("*.example.org", "20170101000000", "20181231235959", "example.org",
                             FetchOutput("local", tmp_path / "data"), playback_workers=1, retries=0)
    return settings, lambda: adapter.build_source(index_directory=tmp_path / "index"), downloads, sessions, responses


def read_records(path):
    with path.open("rb") as stream:
        return [(record.rec_type, record.rec_headers.get_header("WARC-Date"),
                 record.rec_headers.get_header("WARC-Refers-To-Date"), record.raw_stream.read())
                for record in ArchiveIterator(stream, check_digests="raise")]


def test_cc_annual_output_resume_and_reset_keep_years_self_contained(tmp_path, monkeypatch):
    fixtures = [record(timestamp=f"{year}061{day}000000") for year in (2017, 2018) for day in (5, 6)]
    settings, source, downloads, sessions, responses = setup_source(tmp_path, monkeypatch, fixtures)
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.downloads == result.metrics.revisits == 2
    assert len(downloads) == 2
    for year in (2017, 2018):
        path = next(settings.output.data_directory.glob(f"*-{year}-*.warc.gz"))
        records = read_records(path)
        assert [r[0] for r in records] == ["warcinfo", "response", "revisit"]
        assert records[1][3] == b"hello"
        assert records[2][2] == f"{year}-06-15T00:00:00Z"
        index = next(settings.output.data_directory.glob(f"*-{year}-index.cdxj"))
        assert len(index.read_text().splitlines()) == 2
    resumed = run_fetch(settings, source=source())
    assert resumed.metrics.local_reuses == 4 and len(downloads) == 2
    retained = {p: p.read_bytes() for p in settings.output.data_directory.glob("*-2018-*")}
    cache = {p: p.read_bytes() for p in (tmp_path / "index").rglob("*.json")}
    reset = run_fetch(replace(settings, date_end="20171231235959", reset_data=True), source=source())
    assert reset.metrics.downloads == reset.metrics.revisits == 1 and len(downloads) == 3
    assert all(p.read_bytes() == content for p, content in retained.items())
    assert all(p.read_bytes() == content for p, content in cache.items())
    assert all(client.closed for client in sessions)
    assert all(response.closed for response in responses)
    worker_sessions = [client for client in sessions if client.mounts]
    assert len(worker_sessions) == 2  # first run and reset; resume never opens one


def test_invalid_capture_never_seeds_revisit_or_writes_fabricated_response(tmp_path, monkeypatch):
    bad, bad_data = record(timestamp="20170615000000", length_delta=100)
    good, good_data = record(timestamp="20170616000000")
    later, later_data = record(timestamp="20170617000000")
    settings, source, downloads, sessions, responses = setup_source(tmp_path, monkeypatch, [(bad, bad_data), (good, good_data), (later, later_data)])
    result = run_fetch(settings, source=source())
    assert result.metrics.unresolved == 1 and result.metrics.downloads == result.metrics.revisits == 1
    assert [c.identity.timestamp for c in downloads] == [bad.identity.timestamp, good.identity.timestamp]
    records = read_records(next(settings.output.data_directory.glob("*.warc.gz")))
    assert records[1][1] == "2017-06-16T00:00:00Z"
    assert records[2][2] == "2017-06-16T00:00:00Z"
    assert all(response.closed for response in responses)
