import sys

from archive_magic_fetch.console import mirror_console_output, print_progress


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


def test_print_progress_flushes_one_complete_line(capsys):
    print_progress("retry now")
    assert capsys.readouterr().out == "retry now\n"
