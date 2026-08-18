from pathlib import Path

import pytest

from archive_magic_navigator import cli


def write_config(directory: Path, archive_id: str, workspace: Path, *, fallback=True):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "navigator.toml"
    path.write_text(
        f"""
[archive]
id = "{archive_id}"
[source]
type = "local"
directory = "{workspace}"
[playback]
wayback_fallback = {str(fallback).lower()}
""",
        encoding="utf-8",
    )
    return path


def write_remote_config(
    directory: Path,
    archive_id: str,
    *,
    endpoint: str = "https://s3.example",
    region: str = "auto",
):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "navigator.toml"
    path.write_text(
        f"""
[archive]
id = "{archive_id}"
[source]
type = "remote"
bucket = "bucket"
prefix = "{archive_id}"
endpoint_url = "{endpoint}"
region = "{region}"
""",
        encoding="utf-8",
    )
    return path


def test_parse_args_defaults_and_modes(tmp_path):
    request = cli.parse_args([str(tmp_path)])
    assert request.archive == tmp_path
    assert request.catalog is None
    assert request.bind == "127.0.0.1"
    assert request.port == 8080
    assert request.poll_interval_seconds == 60
    assert not hasattr(request, "source")
    assert cli.parse_args(["--catalog", str(tmp_path)]).catalog == tmp_path
    assert cli.parse_args([str(tmp_path), "--wayback-fallback", "off"]).wayback_fallback is False


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["archive", "--catalog", "catalog"],
        ["archive", "--port", "0"],
        ["archive", "--port", "65536"],
        ["archive", "--poll-interval", "0"],
    ),
)
def test_parser_errors_exit_two(arguments):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(arguments)
    assert raised.value.code == 2


def test_help_documents_catalog_and_overrides(capsys):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(["--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--catalog PATH" in output
    assert "--wayback-fallback {on,off}" in output
    assert "--cache" in output
    assert "--poll-interval" in output


def test_main_validates_configuration_and_opens_after_ready(collection_factory, tmp_path, monkeypatch, capsys):
    root, archive, _, _ = collection_factory()
    config = write_config(tmp_path / "config", "example.org", archive)
    events = []

    def fake_run(runtime, bind, port, *, debug, on_ready):
        events.append(("run", (runtime / "config.yaml").is_file()))
        on_ready("http://127.0.0.1:8080/")
        return 0

    monkeypatch.setattr(cli, "run_wayback", fake_run)
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: events.append(("open", url)))
    assert cli.main([str(config), "--open"]) == 0
    assert events == [("run", True), ("open", "http://127.0.0.1:8080/")]
    assert "Serving 1 domain archive with 1 portable collection" in capsys.readouterr().out


def test_fallback_and_cli_override_are_passed_per_archive(collection_factory, tmp_path, monkeypatch):
    _, archive, _, _ = collection_factory()
    config = write_config(tmp_path / "config", "example.org", archive, fallback=False)
    generated = []
    real = cli.build_config

    def capture(archives, *, wayback_fallback):
        generated.append(wayback_fallback)
        return real(archives, wayback_fallback=wayback_fallback)

    monkeypatch.setattr(cli, "build_config", capture)
    monkeypatch.setattr(cli, "run_wayback", lambda *a, **k: 0)
    assert cli.main([str(config)]) == 0
    assert generated == [{"example.org": False}]
    generated.clear()
    assert cli.main([str(config), "--wayback-fallback", "on"]) == 0
    assert generated == [{"example.org": True}]


def test_catalog_is_sorted_and_rejects_duplicate_ids(collection_factory, tmp_path, monkeypatch):
    _, first, _, _ = collection_factory("first")
    _, second, _, _ = collection_factory("second")
    catalog = tmp_path / "catalog"
    write_config(catalog / "b", "second", second)
    write_config(catalog / "a", "first", first)
    captured = []

    def fake_run(runtime, bind, port, *, debug, on_ready):
        captured.append((runtime / "config.yaml").read_text())
        return 0

    monkeypatch.setattr(cli, "run_wayback", fake_run)
    assert cli.main(["--catalog", str(catalog)]) == 0
    assert captured[0].index("first:") < captured[0].index("second:")
    write_config(catalog / "b", "first", second)
    assert cli.main(["--catalog", str(catalog)]) == 1


def test_remote_catalog_requires_one_endpoint_and_region(tmp_path, monkeypatch, capsys):
    catalog = tmp_path / "catalog"
    write_remote_config(catalog / "a", "a.example")
    write_remote_config(
        catalog / "b",
        "b.example",
        endpoint="https://different.example",
    )
    monkeypatch.setattr(cli, "run_wayback", lambda *a, **k: 0)
    assert cli.main(["--catalog", str(catalog)]) == 1
    assert "must share endpoint_url and region" in capsys.readouterr().err


def test_catalog_reports_all_invalid_configurations(tmp_path, capsys):
    catalog = tmp_path / "catalog"
    for name in ("a", "b"):
        directory = catalog / name
        directory.mkdir(parents=True)
        (directory / "navigator.toml").write_text("id = 'x'\n")
    assert cli.main(["--catalog", str(catalog)]) == 1
    error = capsys.readouterr().err
    assert "invalid navigator configuration(s)" in error
    assert str(catalog / "a" / "navigator.toml") in error
    assert str(catalog / "b" / "navigator.toml") in error


def test_catalog_reports_all_invalid_archive_data(tmp_path, capsys):
    catalog = tmp_path / "catalog"
    write_config(catalog / "a", "a.example", tmp_path / "missing-a")
    write_config(catalog / "b", "b.example", tmp_path / "missing-b")
    assert cli.main(["--catalog", str(catalog)]) == 1
    error = capsys.readouterr().err
    assert "a.example:" in error
    assert "b.example:" in error


def test_default_cache_is_beside_configuration(collection_factory, tmp_path):
    _, archive, _, _ = collection_factory()
    config = write_config(tmp_path / "config", "example.org", archive)
    request = cli.parse_args([str(config)])
    settings = cli._load_settings((config,))
    assert cli._cache_directory(request, settings) == (
        config.parent / "navigator-cache"
    ).resolve()
    catalog = tmp_path / "catalog"
    write_config(catalog / "a", "example.org", archive)
    catalog_request = cli.parse_args(["--catalog", str(catalog)])
    catalog_settings = cli._load_settings(cli.discover_configs(catalog))
    assert cli._cache_directory(catalog_request, catalog_settings) == (
        catalog / "navigator-cache"
    ).resolve()


def test_main_warns_for_non_loopback_and_maps_configuration_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run_wayback", lambda *a, **k: 0)
    assert cli.main([str(tmp_path / "missing")]) == 1
    assert "ERROR:" in capsys.readouterr().err
