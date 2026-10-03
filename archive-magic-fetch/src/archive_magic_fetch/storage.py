"""Publish the canonical local archive to an S3-compatible bucket with rclone."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .collection import ArchiveLayout, list_collection_warcs
from .config import FetchOutput
from .index import parse_cdxj_line, validate_cdxj_against_warcs


class PublicationError(RuntimeError):
    """The local archive was retained, but its remote mirror is incomplete."""


@contextmanager
def archive_lock(layout: ArchiveLayout) -> Iterator[None]:
    """Prevent concurrent fetch, reset, and manual publication for one archive."""

    layout.logs_root.mkdir(parents=True, exist_ok=True)
    with (layout.logs_root / ".archive-magic.lock").open("a+b") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublicationError("another fetch or sync owns this archive") from error
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def validate_local_archive(
    layout: ArchiveLayout, *, year: str | None = None
) -> list[Path]:
    """Validate local index locators before allowing mirror deletions."""

    if not layout.root.is_dir():
        raise PublicationError(f"local archive does not exist: {layout.root}")
    if year is not None and not re.fullmatch(r"\d{4}", year):
        raise PublicationError(f"invalid year for publication: {year}")
    prefix = f"{layout.archive_id}-"
    artifacts = sorted(
        path for path in layout.root.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and (year is None or path.name.startswith(f"{prefix}{year}-"))
        and (path.name.endswith(".warc.gz") or path.name.endswith("-index.cdxj"))
    )
    if not artifacts:
        raise PublicationError("local archive is empty; refusing to mirror it")
    indexes = [path for path in artifacts if path.name.endswith("-index.cdxj")]
    if not indexes:
        raise PublicationError("local archive has no CDXJ indexes")
    referenced_all: set[str] = set()
    for index in indexes:
        index_year = index.name.removeprefix(prefix).removesuffix("-index.cdxj")
        if not re.fullmatch(r"\d{4}", index_year):
            raise PublicationError(f"unexpected index name: {index.name}")
        warcs = list_collection_warcs(layout, index_year)
        if not warcs:
            raise PublicationError(f"index has no local WARC: {index.name}")
        lines = [line for line in index.read_text(encoding="utf-8").splitlines() if line]
        try:
            validate_cdxj_against_warcs(layout, index_year, lines)
            referenced = {parse_cdxj_line(line)[2]["filename"] for line in lines}
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationError(f"invalid local index {index.name}: {error}") from error
        if any(not (layout.root / name).is_file() for name in referenced):
            raise PublicationError(f"index references missing WARC: {index.name}")
        referenced_all.update(referenced)
    unindexed = [
        path.name for path in artifacts
        if path.name.endswith(".warc.gz") and path.name not in referenced_all
    ]
    if unindexed:
        raise PublicationError(
            "local WARC has no CDXJ entries: " + ", ".join(sorted(unindexed))
        )
    return artifacts


def _require_aws_credentials() -> None:
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
        "[archive]", "type = s3", f"provider = {provider}",
        "env_auth = true", f"region = {_safe_config_value(output.region, 'region')}",
    ]
    if endpoint:
        lines.append(f"endpoint = {_safe_config_value(endpoint, 'endpoint_url')}")
    return "\n".join(lines) + "\n"


def _remote_path(output: FetchOutput, *, data: bool = True) -> str:
    assert output.bucket is not None
    bucket = _safe_config_value(output.bucket, "bucket")
    if "/" in bucket or ":" in bucket:
        raise PublicationError("invalid bucket for rclone")
    root = f"archive:{bucket}/{output.prefix}".rstrip("/")
    return root + "/data" if data else root


def _run_rclone(config_path: Path, *args: str) -> str:
    command = ["rclone", "--config", str(config_path), *args]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, env=_rclone_env()
        )
        return result.stdout
    except FileNotFoundError as error:
        raise PublicationError("rclone is not installed or not on PATH") from error
    except subprocess.CalledProcessError as error:
        raise PublicationError(f"rclone {args[0]} failed with exit code {error.returncode}: {error.stderr or ''}") from error


@contextmanager
def _temporary_config(output: FetchOutput) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="archive-magic-rclone-") as directory:
        path = Path(directory) / "rclone.conf"
        path.write_text(_rclone_config(output), encoding="utf-8")
        os.chmod(path, 0o600)
        yield path


def sync_archive(
    layout: ArchiveLayout, output: FetchOutput, *, year: str | None = None
) -> None:
    """Mirror only managed root-level archive files, with indexes as commit points."""

    if output.type != "remote":
        return
    validate_local_archive(layout, year=year)
    _require_aws_credentials()
    remote = _remote_path(output)
    source = str(layout.root)
    period = year if year is not None else "????"
    warc_filter = ["--filter", f"+ /{layout.archive_id}-{period}-*.warc.gz", "--filter", "- **"]
    index_filter = ["--filter", f"+ /{layout.archive_id}-{period}-index.cdxj", "--filter", "- **"]
    with _temporary_config(output) as config:
        _reject_legacy_layout(config, output)
        _run_rclone(config, "copy", source, remote, *warc_filter)
        _run_rclone(config, "sync", source, remote, *index_filter)
        _run_rclone(config, "sync", source, remote, *warc_filter)


def _reject_legacy_layout(config: Path, output: FetchOutput) -> None:
    names = _run_rclone(config, "lsf", _remote_path(output, data=False), "--files-only", "--max-depth", "1")
    if any(name.endswith((".warc.gz", ".cdxj")) for name in names.splitlines()):
        raise PublicationError("legacy flat archive data: copy and verify WARC/CDXJ files under data/, then explicitly remove old root objects before sync or reset")


def purge_remote(output: FetchOutput, archive_id: str) -> None:
    """Reset only managed archive data; never purge the bucket or archive root."""
    if output.type != "remote":
        return
    _require_aws_credentials()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", archive_id):
        raise PublicationError("invalid archive ID for reset")
    with _temporary_config(output) as config:
        _reject_legacy_layout(config, output)
        _run_rclone(config, "delete", _remote_path(output),
                    "--filter", f"+ /{archive_id}-????-*.warc.gz",
                    "--filter", f"+ /{archive_id}-????-index.cdxj",
                    "--filter", "- **")
