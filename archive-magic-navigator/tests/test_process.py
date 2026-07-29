from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from archive_magic_navigator.errors import StartupError
from archive_magic_navigator import process


def test_bind_and_url_helpers_cover_loopback_wildcard_and_ipv6():
    assert process.is_loopback_bind("127.0.0.1")
    assert process.is_loopback_bind("::1")
    assert process.is_loopback_bind("localhost")
    assert not process.is_loopback_bind("0.0.0.0")
    assert not process.is_loopback_bind("example.test")
    assert process.landing_url("0.0.0.0", 8080) == "http://127.0.0.1:8080/"
    assert process.landing_url("::", 8080) == "http://[::1]:8080/"


def test_child_command_contains_only_supported_switches(tmp_path):
    command = process.build_command(
        "/venv/bin/wayback",
        tmp_path,
        "127.0.0.1",
        8080,
        debug=True,
    )

    assert command == [
        "/venv/bin/wayback",
        "--directory",
        str(tmp_path),
        "--bind",
        "127.0.0.1",
        "--port",
        "8080",
        "--debug",
    ]
    for forbidden in (
        "--live",
        "--record",
        "--proxy",
        "--enable-auto-fetch",
        "--autoindex",
        "--all-coll",
    ):
        assert forbidden not in command


def test_find_wayback_uses_current_python_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(
        process.metadata,
        "version",
        lambda name: process.PYWB_VERSION,
    )
    monkeypatch.setattr(
        process.sysconfig,
        "get_path",
        lambda name: "/isolated-environment/bin",
    )
    monkeypatch.setattr(
        process.shutil,
        "which",
        lambda name, *, path: calls.append((name, path)) or f"{path}/{name}",
    )

    assert process.find_wayback() == "/isolated-environment/bin/wayback"
    assert calls == [("wayback", "/isolated-environment/bin")]


def test_missing_or_wrong_pywb_installation_is_actionable(monkeypatch):
    monkeypatch.setattr(
        process.metadata,
        "version",
        lambda name: process.PYWB_VERSION,
    )
    monkeypatch.setattr(
        process.sysconfig,
        "get_path",
        lambda name: "/isolated-environment/bin",
    )
    monkeypatch.setattr(
        process.shutil,
        "which",
        lambda name, *, path: None,
    )

    with pytest.raises(StartupError, match="wayback executable not found"):
        process.find_wayback()

    monkeypatch.setattr(process.metadata, "version", lambda name: "9.9.9")
    with pytest.raises(StartupError, match="pywb 2.9.1 is required"):
        process.find_wayback()


class FakeProcess:
    def __init__(self, *, poll_values=(None,), wait_result=0):
        self.poll_values = iter(poll_values)
        self.last_poll = None
        self.wait_result = wait_result
        self.terminated = False
        self.killed = False
        self.stdout = io.BytesIO()

    def poll(self):
        try:
            self.last_poll = next(self.poll_values)
        except StopIteration:
            pass
        return self.last_poll

    def wait(self, timeout=None):
        if isinstance(self.wait_result, BaseException):
            error = self.wait_result
            self.wait_result = 0
            raise error
        self.last_poll = self.wait_result
        return self.wait_result

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.last_poll = -9


def test_early_exit_and_port_conflict_are_translated(
    tmp_path,
    monkeypatch,
):
    capture = FakeCapture("OSError: [Errno 48] Address already in use")
    fake = FakeProcess(poll_values=(None, 1))
    monkeypatch.setattr(process, "_http_ready", lambda url, body: False)

    with pytest.raises(StartupError, match="port 8080 is already in use"):
        process._wait_until_ready(
            fake,
            "http://127.0.0.1:8080/static/readiness-token",
            b"readiness-token",
            1,
            capture,
            "127.0.0.1",
            8080,
        )


def test_readiness_timeout_terminates_child(monkeypatch):
    fake = FakeProcess(poll_values=(None,))
    monkeypatch.setattr(process, "_http_ready", lambda url, body: False)
    ticks = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(process.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(process.time, "sleep", lambda value: None)

    with pytest.raises(StartupError, match="did not become ready"):
        process._wait_until_ready(
            fake,
            "http://127.0.0.1:8080/static/readiness-token",
            b"readiness-token",
            1,
            None,
            "127.0.0.1",
            8080,
        )
    assert fake.terminated


def test_run_wayback_propagates_positive_exit(
    tmp_path,
    monkeypatch,
):
    fake = FakeProcess(poll_values=(None,), wait_result=7)
    monkeypatch.setattr(process, "find_wayback", lambda: "/wayback")
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(process, "_http_ready", lambda url, body: True)
    ready = []

    result = process.run_wayback(
        tmp_path,
        "127.0.0.1",
        8080,
        debug=False,
        on_ready=ready.append,
    )

    assert result == 7
    assert ready == ["http://127.0.0.1:8080/"]


def test_ctrl_c_requests_clean_shutdown(tmp_path, monkeypatch):
    fake = FakeProcess(
        poll_values=(None,),
        wait_result=KeyboardInterrupt(),
    )
    monkeypatch.setattr(process, "find_wayback", lambda: "/wayback")
    monkeypatch.setattr(process.subprocess, "Popen", lambda *a, **k: fake)
    monkeypatch.setattr(process, "_http_ready", lambda url, body: True)

    result = process.run_wayback(
        tmp_path,
        "127.0.0.1",
        8080,
        debug=True,
        on_ready=lambda url: None,
    )

    assert result == 0
    assert fake.terminated


def test_shutdown_kills_after_grace_period():
    fake = FakeProcess(
        poll_values=(None,),
        wait_result=subprocess.TimeoutExpired("wayback", 5),
    )

    process._shutdown(fake)

    assert fake.terminated
    assert fake.killed


class FakeCapture:
    def __init__(self, detail):
        self.detail = detail

    def tail(self, *, wait=False):
        return self.detail


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_readiness_probe_requires_exact_body_and_bypasses_proxies(monkeypatch):
    handlers = []
    bodies = iter((b"another service", b"expected-token"))

    class FakeOpener:
        def open(self, request, timeout):
            return FakeResponse(next(bodies))

    def fake_build_opener(handler):
        handlers.append(handler)
        return FakeOpener()

    monkeypatch.setattr(process, "build_opener", fake_build_opener)

    assert not process._http_ready("http://example.test/probe", b"expected-token")
    assert process._http_ready("http://example.test/probe", b"expected-token")
    assert [handler.proxies for handler in handlers] == [{}, {}]


def test_readiness_marker_keeps_expected_body_secret(tmp_path):
    path, body = process._create_readiness_marker(tmp_path)

    assert body not in path.encode()
    assert (tmp_path / path).read_bytes() == body


def test_output_capture_is_bounded_and_keeps_tail(tmp_path):
    capture = process._OutputCapture(tmp_path / "pywb.log")
    output = b"x" * (process.MAX_CAPTURE_BYTES + 100) + b"\nfinal detail\n"

    capture.start(io.BytesIO(output))
    assert capture.tail(wait=True).endswith("final detail")
    capture.close()

    stored = (tmp_path / "pywb.log").read_bytes()
    assert len(stored) <= process.MAX_CAPTURE_BYTES
    assert stored.endswith(b"final detail\n")
