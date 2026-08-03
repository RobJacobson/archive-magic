from datetime import datetime, timezone

from warcio.archiveiterator import ArchiveIterator
from wayback import CdxRecord

from archive_magic_fetch import job
from archive_magic_fetch.job import FetchRequest


def _timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _capture(urlkey, original, captured, status, digest, length):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=_timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=status,
        digest=digest,
        length=length,
    )


class FakeMemento:
    def __init__(self, capture, *, body, headers):
        self.content = body
        self.headers = headers
        self.timestamp = capture.timestamp
        self.url = capture.original
        self.memento_url = capture.raw_url
        self.status_code = capture.statuscode

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeWaybackClient:
    def __init__(self, primary, target):
        self.primary = primary
        self.target = target
        self.playback_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def close(self):
        return None

    def search(self, url, **kwargs):
        if url == "source.test/":
            assert kwargs["match_type"] == "prefix"
            return iter((self.primary,))
        if url == "https://target.test/":
            assert kwargs["match_type"] == "exact"
            return iter((self.target,))
        raise AssertionError(f"unexpected CDX query: {url}")

    def get_memento(self, capture, **kwargs):
        self.playback_calls.append((capture, kwargs))
        if capture == self.primary:
            return FakeMemento(
                capture,
                body=b"redirect",
                headers={
                    "Content-Type": "text/html",
                    "Location": "https://target.test/",
                },
            )
        if capture == self.target:
            return FakeMemento(
                capture,
                body=b"<html>target locally archived</html>",
                headers={"Content-Type": "text/html"},
            )
        raise AssertionError(capture)


def test_redirect_page_history_is_written_with_primary_in_one_collection(
    tmp_path,
    monkeypatch,
):
    primary = _capture(
        "test,source)/",
        "http://source.test/",
        "20200101000000",
        301,
        "A" * 32,
        100,
    )
    target = _capture(
        "test,target)/",
        "https://target.test/",
        "20191231000000",
        200,
        "B" * 32,
        200,
    )
    client = FakeWaybackClient(primary, target)
    monkeypatch.setattr(job, "_DEFAULT_OUTPUT_ROOT", tmp_path / "archives")
    monkeypatch.setattr(job, "make_client_factory", lambda _agent: lambda: client)

    request = FetchRequest(
        url_pattern="source.test/*",
        date_start="2019",
        date_end="2020",
        warc_mode="all",
        files_mode="none",
        rewrite_local=False,
        redirect_capture="page",
        concurrency=1,
        retries=0,
    )

    assert job.run_fetch(request) is True

    collection = tmp_path / "archives" / "source.test"
    warcs = tuple((collection / "archive").glob("**/*.warc.gz"))
    assert len(warcs) == 1
    targets = []
    with warcs[0].open("rb") as stream:
        for record in ArchiveIterator(stream):
            if record.rec_type == "response":
                targets.append(record.rec_headers.get_header("WARC-Target-URI"))
    assert targets == ["http://source.test/", "https://target.test/"]

    replay = (collection / "replay" / "index.cdxj").read_text()
    assert "http://source.test/" in replay
    assert "https://target.test/" in replay
    assert not (collection / "website").exists()
    assert len(tuple((collection / "sources").iterdir())) == 2
    assert [capture for capture, _kwargs in client.playback_calls] == [
        primary,
        target,
    ]
