from __future__ import annotations

import json

import pytest

from archive_magic_navigator.collections import Collection
from archive_magic_navigator.errors import ValidationError
from archive_magic_navigator import validation


def selected(collection):
    return Collection("example.org", collection.resolve())


def test_valid_index_accepts_integer_and_digit_string_ranges(
    collection_factory,
):
    entries = [
        (
            "org,example)/",
            "20200101000000",
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": 0,
                "length": 8,
            },
        ),
        (
            "org,example)/",
            "20210101000000",
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": "8",
                "length": "8",
            },
        ),
    ]
    _, collection, _, _ = collection_factory(entries=entries)

    summary = validation.validate_collection(selected(collection))

    assert summary.record_count == 2
    assert summary.warc_count == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({"offset": "0", "length": "1"}, "missing required field 'filename'"),
        (
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": True,
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": 1.5,
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": "-1",
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": "0",
                "length": "0",
            },
            "length must be",
        ),
        (
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": "120",
                "length": "16",
            },
            "exceeds WARC size",
        ),
    ),
)
def test_invalid_record_ranges_include_line_context(
    collection_factory,
    payload,
    message,
):
    _, collection, index, _ = collection_factory(
        entries=[("org,example)/", "20200101000000", payload)]
    )

    with pytest.raises(ValidationError, match=message) as raised:
        validation.validate_collection(selected(collection))

    assert str(index) in str(raised.value)
    assert "line 1" in str(raised.value)
    assert "example.org" in str(raised.value)


@pytest.mark.parametrize(
    "filename",
    (
        "/archive/example.org/index.warc.gz",
        "../archive/example.org/index.warc.gz",
        "archive/fixture.warc.gz",
        "archive/../fixture.warc.gz",
        "archive//fixture.warc.gz",
        r"archive\fixture.warc.gz",
        "C:/archive/example.org/index.warc.gz",
        "archive/C:/fixture.warc.gz",
        "https://example.test/fixture.warc.gz",
        "website/fixture.warc.gz",
        "archive/\x00fixture.warc.gz",
    ),
)
def test_unsafe_warc_paths_are_rejected(collection_factory, filename):
    payload = {"filename": filename, "offset": "0", "length": "1"}
    _, collection, _, _ = collection_factory(
        entries=[("org,example)/", "20200101000000", payload)]
    )

    with pytest.raises(ValidationError, match="unsafe WARC filename"):
        validation.validate_collection(selected(collection))


def test_escaping_warc_symlink_is_rejected(collection_factory, tmp_path):
    root, collection, index, warc = collection_factory()
    outside = tmp_path / "outside.warc.gz"
    outside.write_bytes(b"x" * 128)
    warc.unlink()
    warc.symlink_to(outside)

    with pytest.raises(ValidationError, match="escapes or cannot be resolved"):
        validation.validate_collection(selected(collection))


def test_escaping_index_symlink_is_rejected(collection_factory, tmp_path):
    _, collection, index, _ = collection_factory()
    outside = tmp_path / "outside.cdxj"
    index.replace(outside)
    index.symlink_to(outside)

    with pytest.raises(ValidationError, match="index escapes"):
        validation.validate_collection(selected(collection))


def test_index_must_be_nonempty_valid_utf8_and_sorted(collection_factory):
    _, collection, index, _ = collection_factory()
    index.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="index is empty"):
        validation.validate_collection(selected(collection))

    index.write_bytes(b"\xff")
    with pytest.raises(ValidationError, match="valid UTF-8"):
        validation.validate_collection(selected(collection))

    payload = {
        "filename": "archive/example.org/index.warc.gz",
        "offset": "0",
        "length": "1",
    }
    index.write_text(
        f"org,z)/ 20200101000000 {json.dumps(payload)}\n"
        f"org,a)/ 20200101000000 {json.dumps(payload)}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="not sorted"):
        validation.validate_collection(selected(collection))


def test_malformed_lines_timestamp_and_json_fail(collection_factory):
    _, collection, index, _ = collection_factory()
    for content, message in (
        ("only-two fields\n", "expected URL key"),
        ("org,example)/ 2020 {}\n", "14 ASCII digits"),
        ("org,example)/ 20200101000000 []\n", "JSON object"),
        ("org,example)/ 20200101000000 {bad}\n", "invalid JSON"),
    ):
        index.write_text(content, encoding="utf-8")
        with pytest.raises(ValidationError, match=message):
            validation.validate_collection(selected(collection))


def test_each_distinct_warc_is_validated_once(
    collection_factory,
    monkeypatch,
):
    entries = [
        (
            "org,example)/",
            f"20200{number}01000000",
            {
                "filename": "archive/example.org/index.warc.gz",
                "offset": str(number),
                "length": "1",
            },
        )
        for number in range(1, 4)
    ]
    _, collection, _, _ = collection_factory(entries=entries)
    original = validation._validate_warc_path
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(validation, "_validate_warc_path", counted)

    validation.validate_collection(selected(collection))

    assert calls == ["archive/example.org/index.warc.gz"]
