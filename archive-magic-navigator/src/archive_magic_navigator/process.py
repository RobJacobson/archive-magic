"""Start and supervise the separate pywb process."""

from __future__ import annotations

import ipaddress
import secrets
import shutil
import signal
import subprocess
import sysconfig
import threading
import time
from collections.abc import Callable
from importlib import metadata
from pathlib import Path
from typing import BinaryIO
from urllib.request import ProxyHandler, Request, build_opener

from .errors import StartupError


PYWB_VERSION = "2.9.1"
STARTUP_TIMEOUT_SECONDS = 15.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1
LOG_TAIL_LINES = 20
MAX_CAPTURE_BYTES = 256 * 1024
CAPTURE_CHUNK_BYTES = 8192


class _TerminationRequested(Exception):
    pass


class _OutputCapture:
    """Drain child output without allowing diagnostics to grow unbounded."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.path.touch()

    def start(self, stream: BinaryIO) -> None:
        self._thread = threading.Thread(
            target=self._drain,
            args=(stream,),
            daemon=True,
            name="archive-magic-pywb-output",
        )
        self._thread.start()

    def tail(self, *, wait: bool = False) -> str:
        if wait and self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._lock:
            text = bytes(self._buffer).decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-LOG_TAIL_LINES:])

    def close(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._lock:
            output = bytes(self._buffer)
        try:
            self.path.write_bytes(output)
        except OSError:
            pass

    def _drain(self, stream: BinaryIO) -> None:
        try:
            while chunk := stream.read(CAPTURE_CHUNK_BYTES):
                with self._lock:
                    self._buffer.extend(chunk)
                    overflow = len(self._buffer) - MAX_CAPTURE_BYTES
                    if overflow > 0:
                        del self._buffer[:overflow]
        finally:
            stream.close()


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
    """Locate the pinned pywb console script beside the current interpreter."""

    try:
        installed_version = metadata.version("pywb")
    except metadata.PackageNotFoundError as error:
        raise StartupError(
            "pywb is not installed in Navigator's Python environment"
        ) from error
    if installed_version != PYWB_VERSION:
        raise StartupError(
            f"pywb {PYWB_VERSION} is required, but {installed_version} is installed"
        )

    scripts_directory = sysconfig.get_path("scripts")
    if not scripts_directory:
        raise StartupError(
            "cannot locate Navigator's Python scripts directory"
        )
    executable = shutil.which("wayback", path=scripts_directory)
    if executable is None:
        raise StartupError(
            "wayback executable not found in Navigator's Python environment: "
            f"{scripts_directory}"
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
    probe_path, probe_body = _create_readiness_marker(runtime_directory)
    process, capture = _start_wayback(
        command,
        runtime_directory / "pywb.log",
        debug=debug,
    )

    previous_term = _install_termination_handler()
    try:
        url = landing_url(bind, port)
        _wait_until_ready(
            process,
            url + probe_path,
            probe_body,
            startup_timeout,
            capture,
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
        if capture is not None:
            capture.close()


def _start_wayback(
    command: list[str],
    log_path: Path,
    *,
    debug: bool,
) -> tuple[subprocess.Popen[bytes], _OutputCapture | None]:
    capture: _OutputCapture | None = None
    try:
        if not debug:
            capture = _OutputCapture(log_path)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=None if debug else subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        if capture is not None:
            capture.close()
        raise StartupError(f"cannot start wayback: {error}") from error

    if capture is not None:
        if process.stdout is None:
            _shutdown(process)
            capture.close()
            raise StartupError("cannot capture wayback output")
        capture.start(process.stdout)
    return process, capture


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    probe_url: str,
    expected_body: bytes,
    timeout: float,
    capture: _OutputCapture | None,
    bind: str,
    port: int,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            detail = capture.tail(wait=True) if capture is not None else ""
            if _address_in_use(detail):
                raise StartupError(
                    f"port {port} is already in use on {bind}"
                )
            suffix = f"\n{detail}" if detail else ""
            raise StartupError(
                f"wayback exited before becoming ready with status "
                f"{return_code}{suffix}"
            )
        if _http_ready(probe_url, expected_body):
            if process.poll() is None:
                return
            continue
        time.sleep(POLL_INTERVAL_SECONDS)

    _shutdown(process)
    detail = capture.tail(wait=True) if capture is not None else ""
    suffix = f"\n{detail}" if detail else ""
    raise StartupError(
        f"wayback did not become ready within {timeout:g} seconds{suffix}"
    )


def _create_readiness_marker(runtime_directory: Path) -> tuple[str, bytes]:
    filename = f"archive-magic-ready-{secrets.token_urlsafe(18)}.txt"
    body = secrets.token_urlsafe(24)
    static_directory = runtime_directory / "static"
    try:
        static_directory.mkdir(exist_ok=True)
        (static_directory / filename).write_text(body, encoding="ascii")
    except OSError as error:
        raise StartupError(f"cannot create readiness marker: {error}") from error
    return f"static/{filename}", body.encode("ascii")


def _http_ready(url: str, expected_body: bytes) -> bool:
    try:
        request = Request(
            url,
            headers={"User-Agent": "archive-magic-navigator/0.1"},
        )
        opener = build_opener(ProxyHandler({}))
        with opener.open(request, timeout=0.5) as response:
            body = response.read(len(expected_body) + 1)
            return response.status == 200 and body == expected_body
    except (OSError, ValueError):
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
