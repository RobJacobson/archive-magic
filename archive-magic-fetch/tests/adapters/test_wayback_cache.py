"""Complete-year cache publication, validation, and network failure boundaries."""

import json
from pathlib import Path

import archive_magic_fetch.pipeline.discovery.load_or_fetch_year_cdx as cdx
import pytest
from archive_magic_fetch.archive.identity import identity_to_dict
from archive_magic_fetch.models import CaptureRef
from helpers import make_capt


def acquire(tmp_path, **overrides):
    options = dict(
        index_directory=tmp_path / "index",
        year=2004,
        current_year=2026,
        url_pattern="*.example.org",
        sleep=lambda _seconds: None,
    )
    return cdx.load_or_fetch_year_cdx(**(options | overrides))


def capture_json(**overrides):
    return identity_to_dict(make_capt()) | {"mime": "text/html"} | overrides


@pytest.mark.parametrize("empty", [False, True])
def test_complete_year_round_trip_and_cache_hit(tmp_path, monkeypatch, empty):
    captures = () if empty else (CaptureRef(make_capt(), "text/html"),)
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        return cdx._CdxResult(captures, "example.org", "domain")

    monkeypatch.setattr(cdx, "_fetch_cdx", fetch)
    result = acquire(tmp_path)
    path = tmp_path / "index" / "2004.cdx.json"
    assert json.loads(path.read_text()) == ([] if empty else [capture_json()])
    assert list(path.parent.iterdir()) == [path]
    assert requests[0]["date_start"] == "20040101000000"
    assert requests[0]["date_end"] == "20041231235959"
    assert len(requests) == 1
    # Operational settings do not invalidate a complete cache.
    assert (
        acquire(tmp_path, cdx_page_limit=100, cdx_window_days=3).captures
        == result.captures
    )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "content",
    [
        "[",
        "{}",
        "[null]",
        "[{}]",
        "[[]]",
        json.dumps([capture_json(mime=None)]),
        json.dumps([capture_json(original_url="")]),
        json.dumps([capture_json(timestamp=20040615000000)]),
        json.dumps([capture_json(timestamp="20040230000000")]),
        json.dumps([capture_json(timestamp="2004061500000")]),
        json.dumps([capture_json(timestamp="20050615000000")]),
    ],
)
def test_invalid_cache_is_preserved_without_network(tmp_path, monkeypatch, content):
    path = tmp_path / "index" / "2004.cdx.json"
    path.parent.mkdir()
    path.write_text(content)
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **_kw: pytest.fail("CDX requested"))
    with pytest.raises(ValueError, match="invalid CDX cache"):
        acquire(tmp_path)
    assert path.read_text() == content


def test_unreadable_cache_is_not_a_cache_miss(tmp_path, monkeypatch):
    path = tmp_path / "index" / "2004.cdx.json"
    path.parent.mkdir()
    path.write_text("[]")
    original_read = Path.read_text

    def read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("unreadable")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(cdx, "_fetch_cdx", lambda **_kw: pytest.fail("CDX requested"))
    with pytest.raises(ValueError, match="invalid CDX cache.*unreadable"):
        acquire(tmp_path)
    assert original_read(path) == "[]"


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
@pytest.mark.parametrize("boundary", ["write", "rename"])
def test_failed_cache_save_never_publishes_partial_file(
    tmp_path, monkeypatch, failure, boundary
):
    path = tmp_path / "index" / "2004.cdx.json"
    monkeypatch.setattr(
        cdx, "_fetch_cdx", lambda **_kw: cdx._CdxResult((), "example.org", "domain")
    )

    def fail_write(_payload, stream):
        stream.write("[")
        raise failure

    def fail_rename(source, destination):
        assert json.loads(source.read_text()) == []
        assert destination == path
        assert not path.exists()
        raise failure

    with monkeypatch.context() as patch:
        if boundary == "write":
            patch.setattr(cdx.json, "dump", fail_write)
        else:
            patch.setattr(cdx, "publish_file_atomically", fail_rename)
        with pytest.raises(type(failure)):
            acquire(tmp_path)
    assert not path.exists()
    assert list(path.parent.iterdir()) == []
    acquire(tmp_path)
    assert json.loads(path.read_text()) == []


def test_abandoned_temporary_file_is_not_a_cache(tmp_path, monkeypatch):
    directory = tmp_path / "index"
    directory.mkdir()
    abandoned = directory / ".tmp-interrupted.cdx.json.tmp"
    abandoned.write_text("[")
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        return cdx._CdxResult((), "example.org", "domain")

    monkeypatch.setattr(cdx, "_fetch_cdx", fetch)
    acquire(tmp_path)
    assert len(requests) == 1
    assert json.loads((directory / "2004.cdx.json").read_text()) == []


def test_current_year_ignores_existing_cache_and_never_replaces_it(
    tmp_path, monkeypatch
):
    path = tmp_path / "index" / "2004.cdx.json"
    path.parent.mkdir()
    path.write_text("corrupt, but irrelevant for the current year")
    requests = []

    def fetch(**kwargs):
        requests.append(kwargs)
        return cdx._CdxResult((), "example.org", "domain")

    monkeypatch.setattr(cdx, "_fetch_cdx", fetch)
    acquire(tmp_path, current_year=2004)
    acquire(tmp_path, current_year=2004)
    assert len(requests) == 2
    assert all(item["date_end"] == "20041231235959" for item in requests)
    assert path.read_text() == "corrupt, but irrelevant for the current year"


@pytest.mark.parametrize("always_fail", [False, True])
def test_later_page_failure_retries_without_publishing_partial_results(
    tmp_path, monkeypatch, always_fail
):
    from datetime import datetime, timezone

    from wayback import CdxRecord

    requests = []
    path = tmp_path / "index" / "2004.cdx.json"

    class Client:
        def search(self, _url, **kwargs):
            requests.append(kwargs)
            assert not path.exists()
            for day in (1, 2):
                if day == 2 and (always_fail or len(requests) == 1):
                    raise ConnectionError("Connection refused on page two")
                yield CdxRecord(
                    urlkey="org,example)/",
                    original="http://example.org/",
                    timestamp=datetime(2004, 6, day, tzinfo=timezone.utc),
                    statuscode=200,
                    digest="A" * 32,
                    mimetype="text/html",
                    length=5,
                )

        def close(self):
            pass

    monkeypatch.setattr(cdx, "WaybackClient", lambda **_kw: Client())
    if always_fail:
        with pytest.raises(RuntimeError, match="after 10 attempts"):
            acquire(tmp_path)
        assert len(requests) == 10
        assert not path.exists()
    else:
        result = acquire(tmp_path)
        assert len(requests) == 2
        assert [item.identity.timestamp for item in result.captures] == [
            "20040601000000",
            "20040602000000",
        ]
        assert len(json.loads(path.read_text())) == 2
