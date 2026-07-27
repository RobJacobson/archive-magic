import sys
from types import SimpleNamespace

from archive_magic_fetch.console import (
    capture_result_line,
    mirror_console_output,
)


def test_capture_result_line_aligns_http_and_https_capture_urls():
    http_capture = SimpleNamespace(
        original="http://example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "http://example.com/resource"
        ),
    )
    https_capture = SimpleNamespace(
        original="https://example.com/resource",
        view_url=(
            "https://web.archive.org/web/20170101000000/"
            "https://example.com/resource"
        ),
    )

    http_line = capture_result_line(http_capture, "wrote response")
    https_line = capture_result_line(https_capture, "wrote response")

    assert http_line.endswith("resource  : wrote response")
    assert https_line.endswith("resource : wrote response")
    assert http_line.index(": wrote response") == https_line.index(
        ": wrote response"
    )


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
