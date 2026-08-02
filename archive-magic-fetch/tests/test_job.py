from datetime import datetime, timezone
from pathlib import Path

from wayback import CdxRecord

from archive_magic_fetch import cli, job, retry
from archive_magic_fetch.export import ExportResult, ExportSummary
from archive_magic_fetch.files import FilesSummary
from archive_magic_fetch.retrieval import DEFAULT_CONCURRENCY


def timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def record(
    *,
    urlkey="com,example)/",
    original="https://example.com/",
    captured="20000101000000",
    statuscode=200,
    digest="A" * 32,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=statuscode,
        digest=digest,
        length=100,
    )


def request(**overrides):
    values = {
        "url_pattern": "example.com/*",
        "date_start": "1995",
        "date_end": "20260722123456",
        "warc_mode": "all",
        "files_mode": "none",
        "rewrite_local": False,
        "concurrency": DEFAULT_CONCURRENCY,
        "retries": retry.DEFAULT_RETRIES,
    }
    values.update(overrides)
    return job.FetchRequest(**values)


class FakeSession:
    def __init__(self, user_agent):
        self.user_agent = user_agent


class FakeClient:
    def __init__(self, session):
        self.session = session
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args):
        self.exited = True


def install_fake_lifecycle(monkeypatch):
    created = {}

    def make_client_factory(user_agent):
        session = FakeSession(user_agent)
        created["session"] = session
        client = FakeClient(session)
        created["client"] = client
        return lambda: client

    monkeypatch.setattr(job, "make_client_factory", make_client_factory)
    return created


def successful_export(*, failed_urls=(), files_written=0):
    return ExportResult(
        ExportSummary(selected=1),
        (),
        FilesSummary(written=files_written),
        tuple(failed_urls),
    )


def test_run_fetch_owns_client_context_and_passes_same_client(monkeypatch):
    created = install_fake_lifecycle(monkeypatch)
    capture = record()
    layout = object()
    calls = {}

    def fake_discover(
        client,
        pattern,
        start,
        end,
        *,
        progress=None,
        retries=None,
    ):
        calls["discover"] = (
            client,
            pattern,
            start,
            end,
            progress,
            retries,
        )
        return [capture]

    def fake_export(grouped, client, **kwargs):
        calls["export"] = (grouped, client, kwargs)
        return successful_export()

    monkeypatch.setattr(job, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(job, "discover", fake_discover)
    monkeypatch.setattr(
        job,
        "save_acquisition",
        lambda captures, **kwargs: calls.setdefault(
            "provenance",
            (captures, kwargs),
        ),
    )
    monkeypatch.setattr(job, "export_all", fake_export)
    monkeypatch.setattr(
        job,
        "generate_replay_index",
        lambda created, layout: calls.setdefault(
            "replay",
            (created, layout),
        ),
    )
    monkeypatch.setattr(
        job,
        "print_summary",
        lambda summary, **kwargs: calls.setdefault(
            "summary",
            (summary, kwargs),
        ),
    )

    fetch_request = request(url_pattern="*.example.com")
    assert job.run_fetch(fetch_request) is True

    client = created["client"]
    assert created["session"].user_agent == job.USER_AGENT
    assert client.session is created["session"]
    assert client.entered is True
    assert client.exited is True
    assert calls["discover"] == (
        client,
        "*.example.com",
        "1995",
        "20260722123456",
        job._report_discovery_progress,
        retry.DEFAULT_RETRIES,
    )
    assert calls["provenance"][0] == [capture]
    assert calls["export"][0] == {capture.urlkey: [capture]}
    assert calls["export"][1] == client
    assert calls["export"][2]["layout"] is layout
    assert calls["export"][2]["client_factory"] is not None
    assert calls["export"][2]["concurrency"] == DEFAULT_CONCURRENCY
    assert calls["export"][2]["retries"] == retry.DEFAULT_RETRIES
    assert calls["export"][2]["files_mode"] == "none"
    assert calls["replay"] == ((), layout)
    assert calls["summary"][0].selected == 1
    assert calls["summary"][1] == {"warc_mode": "all"}


def test_run_fetch_prints_stage_messages(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    capture = record()
    layout = object()

    monkeypatch.setattr(job, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [capture])
    monkeypatch.setattr(job, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        job,
        "export_all",
        lambda *args, **kwargs: successful_export(),
    )
    monkeypatch.setattr(job, "generate_replay_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(job, "print_summary", lambda *args, **kwargs: None)

    assert job.run_fetch(
        request(url_pattern="*.example.com")
    ) is True
    assert capsys.readouterr().out == (
        "Discovering captures for *.example.com (1995-20260722123456)\n"
        "Discovered 1 captures\n"
        "Saving source acquisition...\n"
        "Grouping 1 captures...\n"
        "Exporting 1 URL groups (concurrency=8)...\n"
        "Building replay index...\n"
    )


def test_run_fetch_plans_and_rewrites_website_files(monkeypatch):
    install_fake_lifecycle(monkeypatch)
    capture = record()
    layout = type("Layout", (), {"website_root": Path("website")})()
    website_plan = object()
    calls = {}

    monkeypatch.setattr(job, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [capture])
    monkeypatch.setattr(job, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        job,
        "preflight_website_layout",
        lambda groups, active_layout, **kwargs: calls.setdefault(
            "plan",
            (groups, active_layout, kwargs),
        )
        and website_plan,
    )

    def fake_export(*args, **kwargs):
        calls["export"] = kwargs
        return successful_export(files_written=1)

    monkeypatch.setattr(job, "export_all", fake_export)
    monkeypatch.setattr(job, "print_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(job, "print_files_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        job,
        "rewrite_local_website",
        lambda root, **kwargs: calls.setdefault(
            "rewrite",
            (root, kwargs),
        ),
    )

    assert job.run_fetch(
        request(
            warc_mode="none",
            files_mode="unique",
            rewrite_local=True,
        )
    ) is True
    assert calls["plan"][1] is layout
    assert calls["plan"][2] == {"include_timestamps": True}
    assert calls["export"]["website_plan"] is website_plan
    assert calls["rewrite"] == (
        layout.website_root,
        {"include_timestamps": True},
    )


def test_run_fetch_lists_failures_after_finalizing_outputs(
    monkeypatch,
    capsys,
):
    install_fake_lifecycle(monkeypatch)
    capture = record()
    layout = object()
    finalized = []

    monkeypatch.setattr(job, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [capture])
    monkeypatch.setattr(job, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        job,
        "export_all",
        lambda *args, **kwargs: successful_export(
            failed_urls=(capture.view_url,)
        ),
    )
    monkeypatch.setattr(
        job,
        "generate_replay_index",
        lambda *args, **kwargs: finalized.append("index"),
    )
    monkeypatch.setattr(job, "print_summary", lambda *args, **kwargs: None)

    assert job.run_fetch(request()) is False
    assert finalized == ["index"]
    assert (
        f"Failed captures:\n{capture.view_url}\n"
        in capsys.readouterr().out
    )


def test_run_fetch_empty_result_is_success(monkeypatch, capsys):
    install_fake_lifecycle(monkeypatch)
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [])

    assert job.run_fetch(request()) is True
    assert capsys.readouterr().out == (
        "Discovering captures for example.com/* (1995-20260722123456)\n"
        "No captures found\n"
    )


def test_run_fetch_does_not_print_summary_when_replay_indexing_fails(
    monkeypatch,
    capsys,
):
    install_fake_lifecycle(monkeypatch)
    selected = record()
    layout = object()
    monkeypatch.setattr(job, "collection_layout", lambda *args, **kwargs: layout)
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [selected])
    monkeypatch.setattr(job, "save_acquisition", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        job,
        "export_all",
        lambda *args, **kwargs: successful_export(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("index failed")

    monkeypatch.setattr(job, "generate_replay_index", fail)

    try:
        job.run_fetch(request())
    except RuntimeError as error:
        assert str(error) == "index failed"
    else:
        raise AssertionError("replay indexing error was not raised")
    assert "Summary:" not in capsys.readouterr().out


def test_main_retains_provenance_log_after_downstream_failure(
    tmp_path,
    monkeypatch,
):
    install_fake_lifecycle(monkeypatch)
    selected = record()
    monkeypatch.setattr(job, "_DEFAULT_OUTPUT_ROOT", tmp_path / "archives")
    monkeypatch.setattr(job, "discover", lambda *args, **kwargs: [selected])
    monkeypatch.setattr(
        job,
        "export_all",
        lambda *args, **kwargs: successful_export(),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("downstream failed")

    monkeypatch.setattr(job, "generate_replay_index", fail)

    assert cli.main(
        ["https://example.com/*", "--end", "20260722123456"]
    ) == 1
    acquisitions = list(
        (tmp_path / "archives" / "example.com" / "sources").iterdir()
    )
    assert len(acquisitions) == 1
    assert (acquisitions[0] / "captures.cdx.gz").exists()
    assert (acquisitions[0] / "query.json").exists()
    log = (acquisitions[0] / "log.txt").read_text()
    assert "Job started:" in log
    assert "Saving source acquisition..." in log
    assert "ERROR: downstream failed" in log
    assert "Job ended:" in log


def test_run_fetch_both_none_is_successful_noop(
    monkeypatch,
    capsys,
):
    def fail(*args, **kwargs):
        raise AssertionError("network client should not be created")

    monkeypatch.setattr(job, "make_client_factory", fail)
    monkeypatch.setattr(job, "collection_layout", fail)

    assert job.run_fetch(
        request(warc_mode="none", files_mode="none")
    ) is True
    assert capsys.readouterr().out == (
        "Nothing to do: both --warc and --files are none\n"
    )


def test_report_discovery_progress(capsys):
    job._report_discovery_progress(2000)
    assert capsys.readouterr().out == "  fetched 2000...\n"
