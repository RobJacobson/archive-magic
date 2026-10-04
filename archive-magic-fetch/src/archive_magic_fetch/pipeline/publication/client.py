"""Shared rclone configuration, invocation, and remote preflight."""

from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.models import PublicationError


def require_aws_credentials() -> None:
    """Fail fast instead of waiting for the AWS SDK to time out on EC2 IMDS."""

    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return
    if os.environ.get("AWS_PROFILE"):
        return
    if (Path.home() / ".aws" / "credentials").is_file():
        return
    raise PublicationError(
        "remote output requires S3-compatible credentials: set "
        "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (Cloudflare R2 access "
        "keys use these names), or AWS_PROFILE / ~/.aws/credentials"
    )


def _rclone_env() -> dict[str, str]:
    env = os.environ.copy()
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def _safe_config_value(value: str, label: str) -> str:
    if not value or any(ord(char) < 32 for char in value):
        raise PublicationError(f"invalid {label} for rclone")
    return value


def _rclone_config(output: FetchOutput) -> str:
    endpoint = output.endpoint_url
    provider = "Other" if endpoint else "AWS"
    lines = [
        "[archive]",
        "type = s3",
        f"provider = {provider}",
        "env_auth = true",
        f"region = {_safe_config_value(output.region, 'region')}",
    ]
    if endpoint:
        lines.append(f"endpoint = {_safe_config_value(endpoint, 'endpoint_url')}")
    return "\n".join(lines) + "\n"


def remote_path(output: FetchOutput, *, data: bool = True) -> str:
    assert output.bucket is not None
    bucket = _safe_config_value(output.bucket, "bucket")
    if "/" in bucket or ":" in bucket:
        raise PublicationError("invalid bucket for rclone")
    root = f"archive:{bucket}/{output.prefix}".rstrip("/")
    return root + "/data" if data else root


def run_rclone(_config_path: Path, *args: str) -> str:
    command = ["rclone", "--config", str(_config_path), *args]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, env=_rclone_env()
        )
        return result.stdout
    except FileNotFoundError as error:
        raise PublicationError("rclone is not installed or not on PATH") from error
    except subprocess.CalledProcessError as error:
        raise PublicationError(
            f"rclone {args[0]} failed with exit code {error.returncode}: {error.stderr or ''}"
        ) from error


@contextmanager
def temporary_config(output: FetchOutput) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="archive-magic-rclone-") as directory:
        path = Path(directory) / "rclone.conf"
        path.write_text(_rclone_config(output), encoding="utf-8")
        os.chmod(path, 0o600)
        yield path


def reject_legacy_layout(config: Path, output: FetchOutput) -> None:
    names = run_rclone(
        config,
        "lsf",
        remote_path(output, data=False),
        "--files-only",
        "--max-depth",
        "1",
    )
    if any(name.endswith((".warc.gz", ".cdxj")) for name in names.splitlines()):
        raise PublicationError(
            "legacy flat archive data: copy and verify WARC/CDXJ files under data/, then explicitly remove old root objects before sync or reset"
        )
