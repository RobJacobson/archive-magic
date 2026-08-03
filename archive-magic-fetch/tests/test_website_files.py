from datetime import datetime, timezone
from pathlib import Path

from wayback import CdxRecord
from wayback.exceptions import MementoPlaybackError

from archive_magic_fetch import collection_paths
from archive_magic_fetch import warc_files
from archive_magic_fetch import website_files
from archive_magic_fetch.search import group_by_url, select_captures


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def capture(
    *,
    original="https://example.com/",
    captured="20170101000000",
    statuscode=200,
    urlkey="com,example)/",
    payload=b"payload",
    digest=None,
    mimetype="text/html",
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype=mimetype,
        statuscode=statuscode,
        digest=digest or ("A" * 32),
        length=len(payload),
    )


class FakeMemento:
    def __init__(
        self,
        *,
        url,
        captured,
        payload=b"payload",
        status_code=200,
        content_type="text/html",
    ):
        self.url = url
        self.timestamp = timestamp(captured)
        self.status_code = status_code
        self.memento_url = f"https://web.archive.org/web/{captured}id_/{url}"
        self.headers = {"Content-Type": content_type}
        self.content = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = []

    def get_memento(self, selected, **kwargs):
        self.calls.append(selected)
        outcome = self.outcomes[selected]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def memento_for(
    selected,
    *,
    payload=b"payload",
    status_code=None,
    content_type="text/html",
):
    return FakeMemento(
        url=selected.original,
        captured=selected.timestamp.astimezone(timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        ),
        payload=payload,
        content_type=content_type,
        status_code=(
            status_code
            if status_code is not None
            else selected.statuscode or 200
        ),
    )


def test_files_latest_writes_website_without_timestamps(tmp_path, capsys):
    root = capture(original="https://example.com/", payload=b"home")
    about = capture(
        original="https://example.com/about",
        urlkey="com,example)/about",
        payload=b"about",
    )
    groups = {
        root.urlkey: [root],
        about.urlkey: [about],
    }
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient(
        {
            root: memento_for(root, payload=b"home"),
            about: memento_for(about, payload=b"about"),
        }
    )

    summary = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="latest",
    ).file_counts

    assert summary.written == 2
    assert (
        layout.website_root / "example.com" / "index.html"
    ).read_bytes() == b"home"
    assert (
        layout.website_root / "example.com" / "about" / "index.html"
    ).read_bytes() == b"about"
    assert not (layout.archive_root).exists()
    assert not (layout.replay_index).exists()
    output = capsys.readouterr().out.splitlines()
    assert output[0] == "Website files: building 2 histories with 8 workers"
    assert len([line for line in output if line.startswith("[")]) == 2
    assert all("1 written, 0 failed" in line for line in output[1:])


def test_files_all_writes_timestamp_directories(tmp_path):
    first = capture(
        original="https://example.com/",
        captured="20051120005053",
        payload=b"old",
    )
    second = capture(
        original="https://example.com/",
        captured="20060715085250",
        payload=b"new",
        digest="B" * 32,
    )
    groups = {first.urlkey: [first, second]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient(
        {
            first: memento_for(first, payload=b"old"),
            second: memento_for(second, payload=b"new"),
        }
    )

    summary = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="all",
    ).file_counts

    assert summary.written == 2
    assert (
        layout.website_root
        / "example.com"
        / "20051120005053"
        / "index.html"
    ).read_bytes() == b"old"
    assert (
        layout.website_root
        / "example.com"
        / "20060715085250"
        / "index.html"
    ).read_bytes() == b"new"


def test_files_only_omits_known_redirect_without_playback(tmp_path):
    redirect = capture(statuscode=301, payload=b"redirect")
    groups = {redirect.urlkey: [redirect]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({})

    summary = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="latest",
    ).file_counts

    assert client.calls == []
    assert summary.written == 0
    assert summary.redirects_omitted == 1
    assert not layout.website_root.exists()


def test_warc_latest_writes_one_response_and_replay(tmp_path):
    older = capture(captured="20100101000000", statuscode=404, payload=b"old")
    newer_200 = capture(
        captured="20150101000000",
        statuscode=200,
        payload=b"ok",
        digest="B" * 32,
    )
    redirect = capture(
        captured="20200101000000",
        statuscode=301,
        digest="C" * 32,
    )
    grouped = group_by_url([older, newer_200, redirect])
    selected = select_captures(grouped, "latest")
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    client = FakeClient({newer_200: memento_for(newer_200, payload=b"ok")})

    result = warc_files.build_warc_files(selected, client, layout=layout)
    from archive_magic_fetch.replay_index import build_replay_index

    build_replay_index(result.built_warcs, layout=layout)

    assert client.calls == [newer_200]
    assert result.warc_counts.responses == 1
    assert layout.replay_index.exists()


def test_dual_mode_reuses_one_representative_download(tmp_path, capsys):
    selected = capture(payload=b"shared-body")
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_plan = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: memento_for(selected, payload=b"shared-body")})
    result = warc_files.build_warc_files(
        groups,
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_plan,
        files_mode="latest",
    )

    assert client.calls == [selected]
    assert result.warc_counts.responses == 1
    assert result.file_counts.written == 1
    assert (
        layout.website_root / "example.com" / "index.html"
    ).read_bytes() == b"shared-body"
    output = capsys.readouterr().out
    assert "archive/example.com/index.warc.gz" in output
    assert "files 1 written, 0 failed" in output
    assert "Website files:" not in output


def test_files_unique_writes_responses_and_skips_revisits(tmp_path):
    first = capture(captured="20170101000000", payload=b"same")
    duplicate = capture(captured="20180101000000", payload=b"same")
    groups = {first.urlkey: [first, duplicate]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_plan = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = warc_files.build_warc_files(
        groups,
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_plan,
        files_mode="unique",
    )

    assert client.calls == [first]
    assert result.warc_counts.responses == 1
    assert result.warc_counts.revisits == 1
    assert result.file_counts.written == 1
    assert (
        layout.website_root
        / "example.com"
        / "20170101000000"
        / "index.html"
    ).read_bytes() == b"same"
    assert not (
        layout.website_root
        / "example.com"
        / "20180101000000"
        / "index.html"
    ).exists()


def test_files_unique_without_warc_uses_same_digest_policy(tmp_path):
    first = capture(captured="20170101000000", payload=b"same")
    duplicate = capture(captured="20180101000000", payload=b"same")
    groups = {first.urlkey: [first, duplicate]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_plan = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_plan,
        warc_mode="none",
        files_mode="unique",
    )

    assert client.calls == [first]
    assert result.built_warcs == ()
    assert result.file_counts.written == 1


def test_files_all_materializes_revisit_body_without_refetch(tmp_path):
    first = capture(captured="20170101000000", payload=b"same")
    duplicate = capture(captured="20180101000000", payload=b"same")
    groups = {first.urlkey: [first, duplicate]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_plan = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = warc_files.build_warc_files(
        groups,
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_plan,
        files_mode="all",
    )

    assert client.calls == [first]
    assert result.file_counts.written == 2
    assert {
        path.read_bytes()
        for path in layout.website_root.rglob("index.html")
    } == {b"same"}
    assert len(list(layout.website_root.rglob("index.html"))) == 2


def test_empty_playback_body_writes_zero_byte_file(tmp_path, capsys):
    selected = capture(
        payload=b"",
        digest="3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ",
    )
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: memento_for(selected, payload=b"")})

    summary = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="latest",
    ).file_counts

    target = layout.website_root / "example.com" / "index.html"
    assert summary.written == 1
    assert summary.playback_failures == 0
    assert target.exists()
    assert target.read_bytes() == b""
    assert "empty playback body" not in capsys.readouterr().out


def test_playback_failure_does_not_create_file(tmp_path):
    selected = capture()
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: MementoPlaybackError("unavailable")})

    summary = warc_files.build_warc_files(
        {},
        client,
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="latest",
    ).file_counts

    assert summary.playback_failures == 1
    assert summary.written == 0
    assert list(Path(layout.website_root).rglob("*")) == []


def test_content_type_path_mismatch_skips_only_loose_file(tmp_path):
    selected = capture(
        original="https://example.com/download/report",
        urlkey="com,example)/download/report",
        mimetype="text/html",
    )
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )

    result = warc_files.build_warc_files(
        groups,
        FakeClient(
            {
                selected: memento_for(
                    selected,
                    content_type="application/pdf; charset=binary",
                )
            }
        ),
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        files_mode="latest",
    )

    assert result.warc_counts.responses == 1
    assert result.file_counts.written == 0
    assert result.file_counts.content_type_mismatches == 1
    assert not layout.website_root.exists()


def test_explicit_extension_is_mime_independent(tmp_path):
    selected = capture(
        original="https://example.com/download/report.pdf",
        urlkey="com,example)/download/report.pdf",
        mimetype="application/pdf",
        payload=b"pdf",
    )
    groups = {selected.urlkey: [selected]}
    layout = collection_paths.collection_paths(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_files = collection_paths.prepare_website_files(
        groups,
        layout,
        include_timestamps=False,
    )

    result = warc_files.build_warc_files(
        {},
        FakeClient(
            {
                selected: memento_for(
                    selected,
                    payload=b"pdf",
                    content_type="application/octet-stream",
                )
            }
        ),
        layout=layout,
        file_captures_by_url=groups,
        website_files=website_files,
        warc_mode="none",
        files_mode="latest",
    )

    assert result.file_counts.written == 1
    assert (
        layout.website_root / "example.com" / "download" / "report.pdf"
    ).read_bytes() == b"pdf"
