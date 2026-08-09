"""Streaming validation for Archive Magic replay indexes."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Never, TextIO

from .collections import Archive, ReplayCollection
from .errors import ValidationError


_TIMESTAMP = re.compile(r"^[0-9]{14}$")
_UNSIGNED_INTEGER = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class ValidationSummary:
    """Compact validation result used for diagnostics and tests."""

    record_count: int
    warc_count: int


@dataclass(frozen=True)
class _LineContext:
    archive_id: str
    collection: ReplayCollection
    index_path: Path
    number: int

    def fail(
        self,
        message: str,
        *,
        cause: Exception | None = None,
    ) -> Never:
        error = ValidationError(
            f"archive {self.archive_id!r}, collection "
            f"{self.collection.collection_id!r}, "
            f"index {self.index_path}, line {self.number}: {message}"
        )
        if cause is None:
            raise error
        raise error from cause


def validate_archive(archive: Archive) -> ValidationSummary:
    """Validate every portable collection exposed by one domain archive."""

    records = 0
    warcs = 0
    for collection in archive.collections:
        summary = validate_collection(collection, archive_id=archive.archive_id)
        records += summary.record_count
        warcs += summary.warc_count
    return ValidationSummary(records, warcs)


def validate_collection(
    collection: ReplayCollection,
    *,
    archive_id: str,
) -> ValidationSummary:
    """Validate one CDXJ and every distinct referenced WARC path."""

    index_path, stream = _open_replay_index(collection, archive_id=archive_id)
    previous: tuple[str, str] | None = None
    warc_sizes: dict[str, int] = {}
    record_count = 0

    try:
        with stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    continue
                record_count += 1
                context = _LineContext(archive_id, collection, index_path, line_number)
                url_key, timestamp, payload = _parse_cdxj_line(context, raw_line)
                current = (url_key, timestamp)
                if previous is not None and current < previous:
                    context.fail("URL key and timestamp are not sorted")
                previous = current
                _validate_record(context, payload, warc_sizes)
    except UnicodeError as error:
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"replay index is not "
            f"valid UTF-8: {index_path}: {error}"
        ) from error
    except OSError as error:
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"cannot read replay "
            f"index: {index_path}: {error}"
        ) from error

    if record_count == 0:
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"replay index is empty: "
            f"{index_path}"
        )
    return ValidationSummary(
        record_count=record_count,
        warc_count=len(warc_sizes),
    )


def _open_replay_index(
    collection: ReplayCollection,
    *,
    archive_id: str,
) -> tuple[Path, TextIO]:
    index_path = collection.replay_index
    other_indexes = sorted(
        path.name
        for path in collection.root.iterdir()
        if path.is_file()
        and path.suffix in {".cdx", ".cdxj"}
        and path != index_path
    )
    if other_indexes:
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"contains unexpected indexes: {', '.join(other_indexes)}"
        )
    try:
        resolved = index_path.resolve(strict=True)
        resolved.relative_to(collection.root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"replay index escapes "
            f"or cannot be resolved: {index_path}"
        ) from error

    stream: TextIO | None = None
    try:
        stream = resolved.open("r", encoding="utf-8")
        mode = os.fstat(stream.fileno()).st_mode
    except OSError as error:
        if stream is not None:
            stream.close()
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"cannot open replay "
            f"index: {index_path}: {error}"
        ) from error
    if not stat.S_ISREG(mode):
        stream.close()
        raise ValidationError(
            f"archive {archive_id!r}, collection {collection.collection_id!r} "
            f"replay index is not a "
            f"regular file: {index_path}"
        )
    return index_path, stream


def _parse_cdxj_line(
    context: _LineContext,
    raw_line: str,
) -> tuple[str, str, dict[str, Any]]:
    parts = raw_line.split(maxsplit=2)
    if len(parts) != 3:
        context.fail("expected URL key, timestamp, and JSON object")
    url_key, timestamp, json_text = parts
    if not _TIMESTAMP.fullmatch(timestamp):
        context.fail("timestamp must contain exactly 14 ASCII digits")
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as error:
        context.fail(f"invalid JSON: {error.msg}")
    if not isinstance(payload, dict):
        context.fail("CDXJ payload must be a JSON object")
    return url_key, timestamp, payload


def _validate_record(
    context: _LineContext,
    payload: dict[str, Any],
    warc_sizes: dict[str, int],
) -> None:
    for field in ("filename", "offset", "length"):
        if field not in payload:
            context.fail(f"missing required field {field!r}")

    filename = payload["filename"]
    if not isinstance(filename, str):
        context.fail("filename must be a string")
    offset = _parse_range_value(
        context,
        "offset",
        payload["offset"],
        allow_zero=True,
    )
    length = _parse_range_value(
        context,
        "length",
        payload["length"],
        allow_zero=False,
    )

    size = warc_sizes.get(filename)
    if size is None:
        size = _validate_warc_path(context, filename)
        warc_sizes[filename] = size
    if offset + length > size:
        context.fail(
            f"indexed range exceeds WARC size for {filename!r}",
        )


def _parse_range_value(
    context: _LineContext,
    field: str,
    value: Any,
    *,
    allow_zero: bool,
) -> int:
    if isinstance(value, str) and _UNSIGNED_INTEGER.fullmatch(value):
        value = int(value)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or (not allow_zero and value == 0)
    ):
        qualifier = "a nonnegative integer" if allow_zero else "a positive integer"
        context.fail(f"{field} must be {qualifier} or digit string")
    return value


def _validate_warc_path(
    context: _LineContext,
    filename: str,
) -> int:
    if (
        not filename
        or "\x00" in filename
        or "\\" in filename
        or PurePosixPath(filename).name != filename
        or PureWindowsPath(filename).drive
    ):
        context.fail(f"unsafe WARC filename {filename!r}")

    expected = re.compile(
        rf"{re.escape(context.archive_id)}-"
        rf"{re.escape(context.collection.collection_id)}-[0-9]{{3}}\.warc\.gz"
    )
    if not expected.fullmatch(filename):
        context.fail(f"foreign or invalid WARC filename {filename!r}")

    candidate = context.collection.root / filename
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(context.collection.root)
    except (OSError, RuntimeError, ValueError) as error:
        context.fail(
            f"WARC filename escapes or cannot be resolved: {filename!r}",
            cause=error,
        )

    try:
        mode = resolved.stat().st_mode
    except OSError as error:
        context.fail(
            f"cannot stat WARC target: {filename!r}",
            cause=error,
        )
    if not stat.S_ISREG(mode):
        context.fail(f"WARC target is not a regular file: {filename!r}")
    try:
        with resolved.open("rb") as stream:
            opened = os.fstat(stream.fileno())
    except OSError as error:
        context.fail(
            f"WARC target is not readable: {filename!r}",
            cause=error,
        )
    if not stat.S_ISREG(opened.st_mode):
        context.fail(f"WARC target is not a regular file: {filename!r}")
    return opened.st_size
