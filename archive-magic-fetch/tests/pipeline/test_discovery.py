from archive_magic_fetch.config.settings import FetchSettings
from helpers import make_source

"""Annual caching integrated with playback, reset, and invocation boundaries."""

import json
from dataclasses import replace
from unittest.mock import MagicMock

import archive_magic_fetch.pipeline.discovery.wayback as cdx
import archive_magic_fetch.pipeline.runner as fetch
import pytest
from archive_magic_fetch.archive.inventory import inventory_collection
from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.models import CaptureRef
from helpers import make_capt, playback


@pytest.fixture
def settings(tmp_path):
    return FetchSettings(
        url_pattern="*.example.org",
        date_start="20040101000000",
        date_end="20041231235959",
        archive_id="example.org",
        output=FetchOutput("local", tmp_path / "data"),
        playback_workers=1,
    )


def run(settings, **kwargs):
    return fetch.run_fetch(
        settings,
        sleep=lambda _s: None,
        source=make_source(
            settings,
            client_factory=lambda: MagicMock(),
            download=kwargs.pop(
                "download_fn", lambda _client, identity: playback(identity)
            ),
        ),
        **kwargs,
    )


def listing(*identities):
    return cdx._CdxResult(
        tuple(CaptureRef(item, "text/html") for item in identities),
        "example.org",
        "domain",
    )


def test_full_year_cache_filters_sorts_dedupes_and_survives_reset(
    settings, monkeypatch
):
    captures = [
        make_capt(ts=timestamp, digest="sha1:" + letter * 32)
        for timestamp, letter in [
            ("20040615000000", "A"),
            ("20040615120000", "B"),
            ("20040615120001", "C"),
            ("20040615235959", "D"),
        ]
    ]
    requests, downloaded = [], []
    path = settings.index_directory / "2004.cdx.json"

    def query(**kwargs):
        requests.append((kwargs["date_start"], kwargs["date_end"]))
        return listing(*reversed(captures), captures[1])

    def download(_client, identity):
        # Historical cache publication precedes even the first WARC download.
        assert len(json.loads(path.read_text())) == 5
        downloaded.append(identity)
        return playback(identity)

    monkeypatch.setattr(cdx, "_fetch_cdx", query)
    narrowed = replace(
        settings, date_start=captures[1].timestamp, date_end=captures[2].timestamp
    )
    assert run(narrowed, download_fn=download).exit_code == 0
    assert downloaded == captures[1:3]
    assert requests == [("20040101000000", "20041231235959")]
    before_reset = path.read_bytes()
    downloaded.clear()
    expanded = run(settings, download_fn=download)
    assert expanded.exit_code == 0
    assert downloaded == [captures[0], captures[3]]
    assert inventory_collection(expanded.layout, "2004").identities == set(captures)
    downloaded.clear()
    assert run(settings, download_fn=download).exit_code == 0
    assert downloaded == []
    assert run(replace(settings, reset_data=True), download_fn=download).exit_code == 0
    assert downloaded == captures
    assert len(requests) == 1
    assert path.read_bytes() == before_reset


def test_empty_selection_reset_clears_year_and_preserves_cache(settings, monkeypatch):
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **_kw: listing(make_capt()))
    initial = run(settings)
    assert initial.exit_code == 0
    assert initial.layout.collection_index("2004").is_file()
    path = settings.index_directory / "2004.cdx.json"
    cached = path.read_bytes()
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **_kw: pytest.fail("CDX requested"))
    empty_selection = replace(
        settings,
        reset_data=True,
        date_start="20040101000000",
        date_end="20040101235959",
    )
    assert run(empty_selection).exit_code == 0
    assert not list(initial.layout.root.glob("*.warc.gz"))
    assert not initial.layout.collection_index("2004").exists()
    assert path.read_bytes() == cached


def test_current_year_repeats_then_caches_after_utc_rollover(settings, monkeypatch):
    requests, downloaded, clock_reads = [], [], []
    clock = ["20041231235959"]
    path = settings.index_directory / "2004.cdx.json"

    def now():
        clock_reads.append(clock[0])
        return clock[0]

    def query(**kwargs):
        requests.append((kwargs["date_start"], kwargs["date_end"]))
        # Midnight during acquisition does not change this invocation's policy.
        clock[0] = "20050101000001"
        return listing(make_capt())

    def download(_client, identity):
        downloaded.append(identity)
        return playback(identity)

    monkeypatch.setattr(fetch, "current_utc_cdx_timestamp", now)
    monkeypatch.setattr(cdx, "_fetch_cdx", query)
    settings = replace(settings, date_end="20060630235959")
    for _ in range(2):
        clock[0] = "20041231235959"
        assert run(settings, download_fn=download).exit_code == 0
        assert not settings.index_directory.exists()
    # Restrict the final invocation to the year that just became historical.
    historical = replace(settings, date_end="20041231235959")
    assert run(historical, download_fn=download).exit_code == 0
    assert path.is_file()
    assert run(historical, download_fn=download).exit_code == 0
    assert requests == [("20040101000000", "20041231235959")] * 3
    assert len(clock_reads) == 4
    assert downloaded == [make_capt()]


@pytest.mark.parametrize("boundary", ["CDX save", "WARC index"])
def test_save_and_warc_failures_have_separate_cache_outcomes(
    settings, monkeypatch, boundary
):
    queries, downloaded = [], []
    path = settings.index_directory / "2004.cdx.json"

    def query(**kwargs):
        queries.append(kwargs)
        return listing(make_capt())

    def download(_client, identity):
        downloaded.append(identity)
        return playback(identity)

    def fail(*_args, **_kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(cdx, "_fetch_cdx", query)
    with monkeypatch.context() as patch:
        if boundary == "CDX save":
            patch.setattr(cdx, "publish_file_atomically", fail)
        else:
            patch.setattr(fetch, "publish_collection_index", fail)
        result = run(settings, download_fn=download)
    assert result.exit_code == 1
    assert result.failed_years == (2004,)
    assert path.exists() is (boundary == "WARC index")
    assert len(downloaded) == (1 if boundary == "WARC index" else 0)
    assert not list(result.layout.root.glob("*.warc.gz"))
    assert not (result.layout.root / ".staging").exists()
    assert (
        "simulated failure" in next(result.layout.logs_root.glob("*.log")).read_text()
    )
    assert run(settings, download_fn=download).exit_code == 0
    assert len(queries) == (1 if boundary == "WARC index" else 2)


@pytest.mark.parametrize("failure", ["corrupt", "429"])
def test_failed_year_continues_without_warc_work(settings, monkeypatch, failure):
    path = settings.index_directory / "2004.cdx.json"
    if failure == "corrupt":
        path.parent.mkdir()
        path.write_text("[")
    requests, sleeps = [], []

    class Client:
        def search(self, _url, **kwargs):
            year = kwargs["from_date"][:4]
            requests.append(year)
            if year == "2004":
                error = RuntimeError("429 Too Many Requests")
                error.status_code = 429
                raise error
            return iter(())

        def close(self):
            pass

    monkeypatch.setattr(cdx, "WaybackClient", lambda **_kw: Client())
    run_settings = replace(settings, date_end="20051231235959")
    result = fetch.run_fetch(
        run_settings,
        sleep=sleeps.append,
        source=make_source(
            run_settings,
            client_factory=lambda: MagicMock(),
            sleep=sleeps.append,
            download=lambda *_args: pytest.fail("incomplete year reached playback"),
        ),
    )
    assert result.exit_code == 1
    assert result.failed_years == (2004,)
    assert requests == (["2004"] * 10 if failure == "429" else []) + ["2005"]
    assert len(sleeps) == (9 if failure == "429" else 0)
    assert json.loads((path.parent / "2005.cdx.json").read_text()) == []
    if failure == "corrupt":
        assert path.read_text() == "["
    else:
        assert not path.exists()
