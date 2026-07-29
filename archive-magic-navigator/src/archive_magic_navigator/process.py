"""Start and supervise the separate pywb process."""

from __future__ import annotations

import ipaddress
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import StartupError


STARTUP_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
LOG_TAIL_LINES = 20


class _TerminationRequested(Exception):
    pass


def is_loopback_bind(address: str) -> bool:
    """Return whether a bind value is clearly local-only."""

    if address.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def probe_host(address: str) -> str:
    """Map wildcard listeners to a usable loopback host."""

    if address == "0.0.0.0":
        return "127.0.0.1"
    if address == "::":
        return "::1"
    return address


def landing_url(address: str, port: int) -> str:
    """Build a browser-safe root URL, including IPv6 brackets."""

    host = probe_host(address)
    try:
        if ipaddress.ip_address(host).version == 6:
            host = f"[{host}]"
    except ValueError:
        pass
    return f"http://{host}:{port}/"


def find_wayback() -> str:
    """Locate the console executable installed with pywb."""

    executable = shutil.which("wayback")
    if executable is None:
        raise StartupError(
            "wayback executable not found; install archive-magic-navigator "
            "with its pywb dependency"
        )
    return executable


def build_command(
    executable: str,
    runtime_directory: Path,
    bind: str,
    port: int,
    *,
    debug: bool,
) -> list[str]:
    """Build the deliberately constrained child command."""

    command = [
        executable,
        "--directory",
        str(runtime_directory),
        "--bind",
        bind,
        "--port",
        str(port),
    ]
    if debug:
        command.append("--debug")
    return command


def run_wayback(
    runtime_directory: Path,
    bind: str,
    port: int,
    *,
    debug: bool,
    on_ready: Callable[[str], None],
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
) -> int:
    """Run pywb until it exits or the parent is interrupted."""

    executable = find_wayback()
    command = build_command(
        executable,
        runtime_directory,
        bind,
        port,
        debug=debug,
    )
    log_path = runtime_directory / "pywb.log"
    log_stream: IO[bytes] | None = None
    output: int | None
    if debug:
        output = None
    else:
        log_stream = log_path.open("wb")
        output = log_stream.fileno()

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        if log_stream is not None:
            log_stream.close()
        raise StartupError(f"cannot start wayback: {error}") from error

    previous_term = _install_termination_handler()
    try:
        url = landing_url(bind, port)
        _wait_until_ready(
            process,
            url,
            startup_timeout,
            log_path if not debug else None,
            bind,
            port,
        )
        on_ready(url)
        return_code = process.wait()
        return _map_exit_code(return_code)
    except (KeyboardInterrupt, _TerminationRequested):
        _shutdown(process)
        return 0
    finally:
        _restore_termination_handler(previous_term)
        if process.poll() is None:
            _shutdown(process)
        if log_stream is not None:
            log_stream.close()


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    url: str,
    timeout: float,
    log_path: Path | None,
    bind: str,
    port: int,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            detail = _log_tail(log_path)
            if _address_in_use(detail):
                raise StartupError(
                    f"port {port} is already in use on {bind}"
                )
            suffix = f"\n{detail}" if detail else ""
            raise StartupError(
                f"wayback exited before becoming ready with status "
                f"{return_code}{suffix}"
            )
        if _http_ready(url):
            return
        time.sleep(POLL_INTERVAL_SECONDS)

    _shutdown(process)
    detail = _log_tail(log_path)
    suffix = f"\n{detail}" if detail else ""
    raise StartupError(
        f"wayback did not become ready within {timeout:g} seconds{suffix}"
    )


def _http_ready(url: str) -> bool:
    request = Request(url, headers={"User-Agent": "archive-magic-navigator/0.1"})
    try:
        with urlopen(request, timeout=0.5) as response:
            return 200 <= response.status < 400
    except HTTPError:
        return False
    except (OSError, URLError):
        return False


def _shutdown(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _map_exit_code(return_code: int) -> int:
    if return_code == 0:
        return 0
    if return_code > 0:
        return return_code
    return 1


def _log_tail(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL_LINES:])


def _address_in_use(detail: str) -> bool:
    lowered = detail.lower()
    return "address already in use" in lowered or "errno 48" in lowered


def _install_termination_handler():
    if not hasattr(signal, "SIGTERM"):
        return None
    try:
        previous = signal.getsignal(signal.SIGTERM)

        def request_termination(signum, frame):
            raise _TerminationRequested

        signal.signal(signal.SIGTERM, request_termination)
        return previous
    except ValueError:
        return None


def _restore_termination_handler(previous) -> None:
    if previous is None or not hasattr(signal, "SIGTERM"):
        return
    signal.signal(signal.SIGTERM, previous)
