"""Streaming validation for Archive Magic replay indexes."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from .collections import Collection
from .errors import ValidationError


_TIMESTAMP = re.compile(r"^[0-9]{14}$")
_UNSIGNED_INTEGER = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class ValidationSummary:
    """Compact validation result used for diagnostics and tests."""

    record_count: int
    warc_count: int


def validate_collection(collection: Collection) -> ValidationSummary:
    """Validate one CDXJ and every distinct referenced WARC path."""

    index_path = collection.replay_index
    try:
        resolved_index = index_path.resolve(strict=True)
        resolved_index.relative_to(collection.root)
    except (OSError, RuntimeError, ValueError) as error:
        raise ValidationError(
            f"collection {collection.collection_id!r} replay index escapes "
            f"or cannot be resolved: {index_path}"
        ) from error
    if not resolved_index.is_file():
        raise ValidationError(
            f"collection {collection.collection_id!r} replay index is not a "
            f"regular file: {index_path}"
        )
    if not os.access(resolved_index, os.R_OK):
        raise ValidationError(
            f"collection {collection.collection_id!r} replay index is not "
            f"readable: {index_path}"
        )

    previous: tuple[str, str] | None = None
    warc_sizes: dict[str, int] = {}
    record_count = 0
    try:
        stream = resolved_index.open("r", encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValidationError(
            f"collection {collection.collection_id!r} cannot open replay "
            f"index: {index_path}: {error}"
        ) from error

    try:
        with stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if not raw_line.strip():
                    continue
                record_count += 1
                parts = raw_line.split(maxsplit=2)
                if len(parts) != 3:
                    _line_error(
                        collection,
                        index_path,
                        line_number,
                        "expected URL key, timestamp, and JSON object",
                    )
                url_key, timestamp, json_text = parts
                if not _TIMESTAMP.fullmatch(timestamp):
                    _line_error(
                        collection,
                        index_path,
                        line_number,
                        "timestamp must contain exactly 14 ASCII digits",
                    )
                current = (url_key, timestamp)
                if previous is not None and current < previous:
                    _line_error(
                        collection,
                        index_path,
                        line_number,
                        "URL key and timestamp are not sorted",
                    )
                previous = current

                try:
                    payload = json.loads(json_text)
                except json.JSONDecodeError as error:
                    _line_error(
                        collection,
                        index_path,
                        line_number,
                        f"invalid JSON: {error.msg}",
                    )
                if not isinstance(payload, dict):
                    _line_error(
                        collection,
                        index_path,
                        line_number,
                        "CDXJ payload must be a JSON object",
                    )
                _validate_record(
                    collection,
                    index_path,
                    line_number,
                    payload,
                    warc_sizes,
                )
    except UnicodeError as error:
        raise ValidationError(
            f"collection {collection.collection_id!r} replay index is not "
            f"valid UTF-8: {index_path}: {error}"
        ) from error
    except OSError as error:
        raise ValidationError(
            f"collection {collection.collection_id!r} cannot read replay "
            f"index: {index_path}: {error}"
        ) from error

    if record_count == 0:
        raise ValidationError(
            f"collection {collection.collection_id!r} replay index is empty: "
            f"{index_path}"
        )
    return ValidationSummary(
        record_count=record_count,
        warc_count=len(warc_sizes),
    )


def _validate_record(
    collection: Collection,
    index_path: Path,
    line_number: int,
    payload: dict[str, Any],
    warc_sizes: dict[str, int],
) -> None:
    for field in ("filename", "offset", "length"):
        if field not in payload:
            _line_error(
                collection,
                index_path,
                line_number,
                f"missing required field {field!r}",
            )

    filename = payload["filename"]
    if not isinstance(filename, str):
        _line_error(
            collection,
            index_path,
            line_number,
            "filename must be a string",
        )
    offset = _parse_range_value(
        collection,
        index_path,
        line_number,
        "offset",
        payload["offset"],
        allow_zero=True,
    )
    length = _parse_range_value(
        collection,
        index_path,
        line_number,
        "length",
        payload["length"],
        allow_zero=False,
    )

    size = warc_sizes.get(filename)
    if size is None:
        size = _validate_warc_path(
            collection,
            index_path,
            line_number,
            filename,
        )
        warc_sizes[filename] = size
    if offset + length > size:
        _line_error(
            collection,
            index_path,
            line_number,
            f"indexed range exceeds WARC size for {filename!r}",
        )


def _parse_range_value(
    collection: Collection,
    index_path: Path,
    line_number: int,
    field: str,
    value: Any,
    *,
    allow_zero: bool,
) -> int:
    if isinstance(value, bool):
        valid = False
    elif isinstance(value, int):
        valid = True
    elif isinstance(value, str) and _UNSIGNED_INTEGER.fullmatch(value):
        valid = True
        value = int(value)
    else:
        valid = False
    if not valid or value < 0 or (not allow_zero and value == 0):
        qualifier = "a nonnegative integer" if allow_zero else "a positive integer"
        _line_error(
            collection,
            index_path,
            line_number,
            f"{field} must be {qualifier} or digit string",
        )
    return value


def _validate_warc_path(
    collection: Collection,
    index_path: Path,
    line_number: int,
    filename: str,
) -> int:
    parts = filename.split("/")
    windows_path = PureWindowsPath(filename)
    parsed = urlsplit(filename)
    if (
        not filename
        or "\x00" in filename
        or "\\" in filename
        or filename.startswith("/")
        or windows_path.drive
        or any(PureWindowsPath(part).drive for part in parts)
        or parsed.scheme
        or any(part in {"", ".", ".."} for part in parts)
        or parts[0] != "archive"
    ):
        _line_error(
            collection,
            index_path,
            line_number,
            f"unsafe WARC filename {filename!r}",
        )

    pure_path = PurePosixPath(filename)
    candidate = collection.root.joinpath(*pure_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(collection.root)
    except (OSError, RuntimeError, ValueError) as error:
        _line_error(
            collection,
            index_path,
            line_number,
            f"WARC filename escapes or cannot be resolved: {filename!r}",
            cause=error,
        )
    if not resolved.is_file():
        _line_error(
            collection,
            index_path,
            line_number,
            f"WARC target is not a regular file: {filename!r}",
        )
    if not os.access(resolved, os.R_OK):
        _line_error(
            collection,
            index_path,
            line_number,
            f"WARC target is not readable: {filename!r}",
        )
    try:
        return resolved.stat().st_size
    except OSError as error:
        _line_error(
            collection,
            index_path,
            line_number,
            f"cannot stat WARC target: {filename!r}",
            cause=error,
        )


def _line_error(
    collection: Collection,
    index_path: Path,
    line_number: int,
    message: str,
    *,
    cause: Exception | None = None,
) -> None:
    error = ValidationError(
        f"collection {collection.collection_id!r}, index {index_path}, "
        f"line {line_number}: {message}"
    )
    if cause is None:
        raise error
    raise error from cause
