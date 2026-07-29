from pathlib import Path

import pytest

from archive_magic_navigator import cli


def test_parse_args_defaults_and_modes():
    request = cli.parse_args(["example.org"])

    assert request == cli.NavigatorRequest(
        collection_id="example.org",
        archives=Path("./archives"),
        bind="127.0.0.1",
        port=8080,
        open_browser=False,
        debug=False,
    )
    assert cli.parse_args(["--all"]).collection_id is None


@pytest.mark.parametrize(
    "arguments",
    (
        [],
        ["example.org", "--all"],
        ["example.org", "--port", "0"],
        ["example.org", "--port", "65536"],
        ["example.org", "--port", "not-a-port"],
        ["example.org", "--bind", ""],
    ),
)
def test_parser_errors_exit_two(arguments):
    with pytest.raises(SystemExit) as raised:
        cli.parse_args(arguments)
    assert raised.value.code == 2


def test_main_validates_before_start_and_opens_after_ready(
    collection_factory,
    monkeypatch,
    capsys,
):
    root, _, _, _ = collection_factory()
    events = []

    def fake_run(runtime, bind, port, *, debug, on_ready):
        events.append(("run", (runtime / "config.yaml").is_file()))
        on_ready("http://127.0.0.1:8080/")
        return 0

    monkeypatch.setattr(cli, "run_wayback", fake_run)
    monkeypatch.setattr(
        cli.webbrowser,
        "open",
        lambda url: events.append(("open", url)),
    )

    result = cli.main(
        [
            "example.org",
            "--archives",
            str(root),
            "--open",
        ]
    )

    assert result == 0
    assert events == [
        ("run", True),
        ("open", "http://127.0.0.1:8080/"),
    ]
    assert "Serving 1 collection" in capsys.readouterr().out


def test_main_warns_for_non_loopback_bind(
    collection_factory,
    monkeypatch,
    capsys,
):
    root, _, _, _ = collection_factory()
    monkeypatch.setattr(cli, "run_wayback", lambda *args, **kwargs: 0)

    assert (
        cli.main(
            [
                "example.org",
                "--archives",
                str(root),
                "--bind",
                "0.0.0.0",
            ]
        )
        == 0
    )
    assert "WARNING: non-loopback" in capsys.readouterr().err


def test_main_maps_validation_failure_without_traceback(tmp_path, capsys):
    result = cli.main(
        ["missing", "--archives", str(tmp_path / "does-not-exist")]
    )

    assert result == 1
    output = capsys.readouterr()
    assert output.err.startswith("ERROR:")
    assert "Traceback" not in output.err


def test_all_mode_aggregates_invalid_collection_diagnostics(
    collection_factory,
    capsys,
):
    root, _, index, _ = collection_factory("collection-a")
    _, _, second_index, _ = collection_factory("collection-b")
    index.write_text("", encoding="utf-8")
    second_index.write_text("malformed\n", encoding="utf-8")

    assert cli.main(["--all", "--archives", str(root)]) == 1

    error = capsys.readouterr().err
    assert "invalid collections:" in error
    assert "collection 'collection-a'" in error
    assert "collection 'collection-b'" in error
