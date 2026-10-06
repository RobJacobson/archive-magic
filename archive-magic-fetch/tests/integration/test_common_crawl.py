"""Exercise the real CC adapter through annual publication-ready local output."""

import json
from dataclasses import replace

import pytest
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


@pytest.mark.parametrize("formats", [("warc", "warc"), ("arc", "arc"), ("arc", "warc"), ("warc", "arc")])
def test_cc_annual_output_resume_and_reset_keep_years_self_contained(tmp_path, monkeypatch, formats):
    fixtures = [record(timestamp=f"{year}061{day}000000", format=formats[day - 5])
                for year in (2017, 2018) for day in (5, 6)]
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


@pytest.mark.parametrize("format", ["warc", "arc"])
def test_invalid_capture_never_seeds_revisit_or_writes_fabricated_response(tmp_path, monkeypatch, format):
    bad, bad_data = record(timestamp="20170615000000", length_delta=100, format=format)
    if format == "arc":
        bad = replace(bad, identity=replace(bad.identity, payload_digest="-"))
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


def test_mixed_formats_preserve_payload_and_source_uri(tmp_path, monkeypatch):
    fixtures = [record(timestamp=f"2017061{day}000000", format=format, body=format.encode())
                for day, format in [(5, "arc"), (6, "warc")]]
    settings, source, downloads, _, _ = setup_source(tmp_path, monkeypatch, fixtures)
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.downloads == 2
    path = next(settings.output.data_directory.glob("*.warc.gz"))
    with path.open("rb") as stream:
        records = ArchiveIterator(stream, check_digests="raise")
        next(records).raw_stream.read()  # warcinfo
        for capture, _ in fixtures:
            stored = next(records)
            assert stored.rec_headers.protocol == "WARC/1.1"
            assert stored.rec_headers.get_header("WARC-Source-URI") == "https://data.commoncrawl.org/" + capture.locator.filename
            assert stored.raw_stream.read() == (b"arc" if capture.locator.filename.endswith(".arc.gz") else b"warc")
        assert next(records, None) is None
    resumed = run_fetch(settings, source=source())
    assert resumed.metrics.local_reuses == 2 and len(downloads) == 2


@pytest.mark.parametrize("digest", ["-", "sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"])
def test_unmatched_arc_payload_never_seeds_reuse(tmp_path, monkeypatch, digest):
    fixtures = [record(timestamp=f"2017061{day}000000", format="arc") for day in (5, 6)]
    fixtures = [(replace(c, identity=replace(c.identity, payload_digest=digest)), data) for c, data in fixtures]
    settings, source, downloads, _, _ = setup_source(tmp_path, monkeypatch, fixtures)
    result = run_fetch(settings, source=source())
    assert result.metrics.downloads == result.metrics.digest_mismatch_accepted == len(downloads) == 2
    assert result.metrics.revisits == 0


def test_unsupported_and_truncated_records_show_reasons_without_changing_exit_code(tmp_path, monkeypatch, capsys):
    fixtures = [record(kind="resource"), record(format="arc", timestamp="20170616000000",
                headers=(("x-commoncrawl-ContentTruncated", "TruncatedInDownload"),))]
    settings, source, downloads, _, _ = setup_source(tmp_path, monkeypatch, fixtures)
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.unresolved == 2
    assert result.metrics.downloads == result.metrics.revisits == 0
    assert len(downloads) == 2
    output = capsys.readouterr().out
    assert "Ignored [unsupported source record type: resource]" in output
    assert "Ignored [source ARC declares truncation]" in output
    log = json.loads(next(result.layout.logs_root.glob("*.json")).read_text())
    failures = log["years"]["2017"]["failures"]
    assert [(f["category"], f["message"]) for f in failures] == [
        ("unavailable", "unsupported source record type: resource"),
        ("truncated", "source ARC declares truncation"),
    ]


def test_verified_recovery_mixed_run_metadata_reuse_and_resume(tmp_path, monkeypatch, capsys):
    from archive_magic_fetch.archive.identity import payload_digest

    fixtures = [
        record(format="arc", timestamp="20170615000000", body=b"ordinary"),
        record(format="arc", timestamp="20170616000000", body=b"repaired", length_delta=100),
        record(format="arc", timestamp="20170617000000", body=b"repaired"),
        record(format="arc", timestamp="20170618000000", body=b"unverified", length_delta=100),
        record(format="arc", timestamp="20170619000000", body=b"later"),
    ]
    bad, data = fixtures[3]
    fixtures[3] = (replace(bad, identity=replace(bad.identity, payload_digest=payload_digest(b"different"))), data)
    settings, source, downloads, _, responses = setup_source(tmp_path, monkeypatch, fixtures)
    settings = replace(settings, retries=4)
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.represented == 4
    assert result.metrics.downloads == 3 and result.metrics.revisits == 1
    assert result.metrics.source_recovered == 1 and result.metrics.unresolved == 1
    assert len(downloads) == 4  # bad ARC is attempted once, revisit has no HTTP request
    output = capsys.readouterr().out
    assert "recovered: arc-block-length" in output
    assert "Ignored [ARC recovery could not verify payload]" in output
    assert "source-recovered=1" in output
    path = next(settings.output.data_directory.glob("*.warc.gz"))
    with path.open("rb") as stream:
        stored = [(r.rec_type, r.rec_headers.get_header("Archive-Magic-Source-Repairs"), r.raw_stream.read())
                  for r in ArchiveIterator(stream, check_digests="raise")]
    assert stored[1:] == [
        ("response", None, b"ordinary"), ("response", "arc-block-length", b"repaired"),
        ("revisit", None, b""), ("response", None, b"later"),
    ]
    log = json.loads(next(result.layout.logs_root.glob("*.json")).read_text())
    assert log["years"]["2017"]["counts"]["source_recovered"] == 1
    assert log["years"]["2017"]["failures"][0]["category"] == "unavailable"
    before = path.read_bytes()
    resumed = run_fetch(settings, source=source())
    assert resumed.metrics.local_reuses == 4
    assert resumed.metrics.source_recovered == resumed.metrics.downloads == 0
    assert len(downloads) == 5  # only the unresolved capture is retried on a new run
    assert path.read_bytes() == before
    assert all(response.closed for response in responses)


@pytest.mark.parametrize("indexed_url", ["https://example.org/a%20path", "https://example.org/a path"])
def test_url_recovery_output_and_resume(tmp_path, monkeypatch, indexed_url):
    import gzip
    from archive_magic_fetch.archive.identity import make_identity

    capture, data = record(format="arc")
    raw = gzip.decompress(data).replace(b"https://example.org/", b"https://example.org/a path", 1)
    data = gzip.compress(raw)
    capture = replace(capture, identity=make_identity(original_url=indexed_url,
        timestamp=capture.identity.timestamp, status_token="200", payload_digest=capture.identity.payload_digest),
        locator=replace(capture.locator, length=len(data)))
    later, later_data = record(format="arc", timestamp="20170616000000")
    later = replace(later, identity=replace(capture.identity, timestamp=later.identity.timestamp))
    settings, source, downloads, _, _ = setup_source(tmp_path, monkeypatch, [(capture, data), (later, later_data)])
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.source_recovered == 1
    assert result.metrics.revisits == 1
    path = next(settings.output.data_directory.glob("*.warc.gz"))
    with path.open("rb") as stream:
        records = ArchiveIterator(stream, check_digests="raise")
        next(records).raw_stream.read()
        response = next(records)
        assert response.rec_headers.get_header("Archive-Magic-Source-Repairs") == "arc-url-spaces"
        assert response.rec_headers.get_header("WARC-Target-URI") == "https://example.org/a%20path"
        assert response.raw_stream.read() == b"hello"
        revisit = next(records)
        assert revisit.rec_type == "revisit"
        assert revisit.rec_headers.get_header("WARC-Refers-To-Target-URI") == "https://example.org/a%20path"
        revisit.raw_stream.read()
    index = next(settings.output.data_directory.glob("*-index.cdxj"))
    assert [json.loads(line.split(" ", 2)[2])["url"] for line in index.read_text().splitlines()] == [indexed_url] * 2
    before = path.read_bytes()
    resumed = run_fetch(settings, source=source())
    assert resumed.exit_code == 0 and resumed.metrics.local_reuses == 2
    assert resumed.metrics.source_recovered == 0 and len(downloads) == 1
    assert path.read_bytes() == before


def test_permanent_warc_failures_allow_later_captures_and_resume(tmp_path, monkeypatch, capsys):
    from archive_magic_fetch.archive.identity import payload_digest

    options = [
        {"length_delta": 100},
        {"warc_headers": (("WARC-Payload-Digest", payload_digest(b"wrong")),)},
        {"headers": (("Bad Name", "value"),)},
        {"headers": (("x-commoncrawl-ContentTruncated", "TruncatedInDownload"),)},
        {"warc_headers": (("X-Large", "x" * 65536),)},
        {}, {},
    ]
    fixtures = [record(timestamp=f"201706{day:02d}000000", **kw) for day, kw in enumerate(options, 15)]
    settings, source, downloads, _, responses = setup_source(tmp_path, monkeypatch, fixtures)
    settings = replace(settings, retries=4)
    result = run_fetch(settings, source=source())
    assert result.exit_code == 0 and result.metrics.unresolved == 5
    assert result.metrics.downloads == result.metrics.revisits == 1
    assert result.metrics.source_recovered == 0
    assert [c.identity.timestamp for c in downloads] == [c.identity.timestamp for c, _ in fixtures[:-1]]
    output = capsys.readouterr().out
    assert output.count("Ignored [malformed WARC]") == 3
    assert "Ignored [source WARC declares truncation]" in output
    assert "Ignored [archive record exceeds size limit]" in output
    log = json.loads(next(result.layout.logs_root.glob("*.json")).read_text())
    failures = log["years"]["2017"]["failures"]
    assert [failure["category"] for failure in failures] == ["unavailable"] * 3 + ["truncated", "unavailable"]
    assert "WARC payload digest mismatch" in failures[1]["message"]
    path = next(settings.output.data_directory.glob("*.warc.gz"))
    stored = read_records(path)
    assert [r[0] for r in stored] == ["warcinfo", "response", "revisit"]
    assert stored[1][1] == "2017-06-20T00:00:00Z" and stored[1][3] == b"hello"
    assert stored[2][2] == stored[1][1]
    before = path.read_bytes()
    resumed = run_fetch(settings, source=source())
    assert resumed.exit_code == 0 and resumed.metrics.local_reuses == 2
    assert resumed.metrics.downloads == resumed.metrics.revisits == 0
    assert len(downloads) == 11  # Each unresolved capture gets one attempt on the new run.
    assert path.read_bytes() == before and all(response.closed for response in responses)
