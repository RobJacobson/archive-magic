from datetime import datetime, timezone
from pathlib import Path

from wayback import CdxRecord
from wayback.exceptions import MementoPlaybackError

from archive_magic_fetch import export, files, paths
from archive_magic_fetch.discovery import apply_output_mode, group_captures


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
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest=digest or ("A" * 32),
        length=len(payload),
    )


class FakeMemento:
    def __init__(self, *, url, captured, payload=b"payload", status_code=200):
        self.url = url
        self.timestamp = timestamp(captured)
        self.status_code = status_code
        self.memento_url = f"https://web.archive.org/web/{captured}id_/{url}"
        self.headers = {"Content-Type": "text/html"}
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


def memento_for(selected, *, payload=b"payload", status_code=None):
    return FakeMemento(
        url=selected.original,
        captured=selected.timestamp.astimezone(timezone.utc).strftime(
            "%Y%m%d%H%M%S"
        ),
        payload=payload,
        status_code=(
            status_code
            if status_code is not None
            else selected.statuscode or 200
        ),
    )


def test_files_latest_writes_website_without_timestamps(tmp_path):
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_website_layout(
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

    summary = export.export_all(
        {},
        (),
        client,
        file_capture_groups=groups,
        website_plan=plan,
        warc_mode="none",
        files_mode="latest",
    ).files_summary

    assert summary.written == 2
    assert (
        layout.website_root / "example.com" / "index.html"
    ).read_bytes() == b"home"
    assert (
        layout.website_root / "example.com" / "about" / "index.html"
    ).read_bytes() == b"about"
    assert not (layout.archive_root).exists()
    assert not (layout.replay_index).exists()


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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_website_layout(
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

    summary = export.export_all(
        {},
        (),
        client,
        file_capture_groups=groups,
        website_plan=plan,
        warc_mode="none",
        files_mode="all",
    ).files_summary

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
    grouped = group_captures([older, newer_200, redirect])
    selected = apply_output_mode(grouped, "latest")
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_layout(selected, layout)
    client = FakeClient({newer_200: memento_for(newer_200, payload=b"ok")})

    result = export.export_all(selected, plan.buckets, client)
    from archive_magic_fetch.replay import generate_replay_index

    generate_replay_index(result.created_warcs, layout=layout)

    assert client.calls == [newer_200]
    assert result.summary.responses == 1
    assert layout.replay_index.exists()


def test_dual_mode_reuses_one_representative_download(tmp_path):
    selected = capture(payload=b"shared-body")
    groups = {selected.urlkey: [selected]}
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    warc_plan = paths.preflight_layout(groups, layout)
    website_plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: memento_for(selected, payload=b"shared-body")})
    result = export.export_all(
        groups,
        warc_plan.buckets,
        client,
        file_capture_groups=groups,
        website_plan=website_plan,
        files_mode="latest",
    )

    assert client.calls == [selected]
    assert result.summary.responses == 1
    assert result.files_summary.written == 1
    assert (
        layout.website_root / "example.com" / "index.html"
    ).read_bytes() == b"shared-body"


def test_files_unique_writes_responses_and_skips_revisits(tmp_path):
    first = capture(captured="20170101000000", payload=b"same")
    duplicate = capture(captured="20180101000000", payload=b"same")
    groups = {first.urlkey: [first, duplicate]}
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    warc_plan = paths.preflight_layout(groups, layout)
    website_plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = export.export_all(
        groups,
        warc_plan.buckets,
        client,
        file_capture_groups=groups,
        website_plan=website_plan,
        files_mode="unique",
    )

    assert client.calls == [first]
    assert result.summary.responses == 1
    assert result.summary.revisits == 1
    assert result.files_summary.written == 1
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    website_plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = export.export_all(
        {},
        (),
        client,
        file_capture_groups=groups,
        website_plan=website_plan,
        warc_mode="none",
        files_mode="unique",
    )

    assert client.calls == [first]
    assert result.created_warcs == ()
    assert result.files_summary.written == 1


def test_files_all_materializes_revisit_body_without_refetch(tmp_path):
    first = capture(captured="20170101000000", payload=b"same")
    duplicate = capture(captured="20180101000000", payload=b"same")
    groups = {first.urlkey: [first, duplicate]}
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    warc_plan = paths.preflight_layout(groups, layout)
    website_plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=True,
    )
    client = FakeClient({first: memento_for(first, payload=b"same")})

    result = export.export_all(
        groups,
        warc_plan.buckets,
        client,
        file_capture_groups=groups,
        website_plan=website_plan,
        files_mode="all",
    )

    assert client.calls == [first]
    assert result.files_summary.written == 2
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
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: memento_for(selected, payload=b"")})

    summary = export.export_all(
        {},
        (),
        client,
        file_capture_groups=groups,
        website_plan=plan,
        warc_mode="none",
        files_mode="latest",
    ).files_summary

    target = layout.website_root / "example.com" / "index.html"
    assert summary.written == 1
    assert summary.playback_failures == 0
    assert target.exists()
    assert target.read_bytes() == b""
    assert "empty playback body" not in capsys.readouterr().out


def test_playback_failure_does_not_create_file(tmp_path):
    selected = capture()
    groups = {selected.urlkey: [selected]}
    layout = paths.collection_layout(
        "https://example.com/*",
        root=tmp_path / "archives",
    )
    plan = paths.preflight_website_layout(
        groups,
        layout,
        include_timestamps=False,
    )
    client = FakeClient({selected: MementoPlaybackError("unavailable")})

    summary = export.export_all(
        {},
        (),
        client,
        file_capture_groups=groups,
        website_plan=plan,
        warc_mode="none",
        files_mode="latest",
    ).files_summary

    assert summary.playback_failures == 1
    assert summary.written == 0
    assert list(Path(layout.website_root).rglob("*")) == []


def test_files_summary_includes_playback_failure_categories(capsys):
    summary = files.FilesSummary(
        written=4,
        playback_failures=3,
        invalid_content_encoding_failures=1,
        truncated_response_failures=1,
    )

    files.print_files_summary(summary, files_mode="all")

    assert capsys.readouterr().out == (
        "Files: 4 written (all); 3 playback failures "
        "(1 invalid content encoding, 1 truncated response, 1 other); "
        "0 redirects omitted\n"
    )
