import sys
from types import SimpleNamespace

from archive_magic_fetch.console import (
    capture_result_line,
    mirror_console_output,
)


def test_capture_result_line_aligns_scheme_and_www_variants():
    http_www_capture = SimpleNamespace(
        original="http://www.example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "http://www.example.com/resource"
        ),
    )
    https_www_capture = SimpleNamespace(
        original="https://www.example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "https://www.example.com/resource"
        ),
    )
    http_apex_capture = SimpleNamespace(
        original="http://example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "http://example.com/resource"
        ),
    )
    https_apex_capture = SimpleNamespace(
        original="https://example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "https://example.com/resource"
        ),
    )

    lines = [
        capture_result_line(capture, "wrote response")
        for capture in (
            http_www_capture,
            https_www_capture,
            http_apex_capture,
            https_apex_capture,
        )
    ]

    assert lines[0].endswith("resource  : wrote response")
    assert lines[1].endswith("resource : wrote response")
    assert lines[2].endswith("resource      : wrote response")
    assert lines[3].endswith("resource     : wrote response")
    assert len({line.index(": wrote response") for line in lines}) == 1


def test_console_mirror_logs_stdout_and_stderr_before_and_after_attach(
    tmp_path,
    capsys,
):
    log_path = tmp_path / "log.txt"

    with mirror_console_output() as mirror:
        print("before attach")
        print("warning", file=sys.stderr)
        mirror.attach(log_path)
        print("after attach")
        assert log_path.read_text() == (
            "before attach\n"
            "warning\n"
            "after attach\n"
        )

    output = capsys.readouterr()
    assert output.out == "before attach\nafter attach\n"
    assert output.err == "warning\n"
