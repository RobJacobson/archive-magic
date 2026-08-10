from __future__ import annotations

import json

import pytest

from archive_magic_navigator.collections import ReplayCollection
from archive_magic_navigator.errors import ValidationError
from archive_magic_navigator import validation


def selected(collection):
    root = collection.resolve() / "collections" / "2020"
    return ReplayCollection(
        "2020", root, root / "example.org-2020-index.cdxj"
    )


def validate(collection):
    return validation.validate_collection(
        selected(collection), archive_id="example.org"
    )


def test_valid_index_accepts_integer_and_digit_string_ranges(
    collection_factory,
):
    entries = [
        (
            "org,example)/",
            "20200101000000",
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": 0,
                "length": 8,
            },
        ),
        (
            "org,example)/",
            "20210101000000",
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": "8",
                "length": "8",
            },
        ),
    ]
    _, collection, _, _ = collection_factory(entries=entries)

    summary = validation.validate_collection(
        selected(collection), archive_id="example.org"
    )

    assert summary.record_count == 2
    assert summary.warc_count == 1


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({"offset": "0", "length": "1"}, "missing required field 'filename'"),
        (
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": True,
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": 1.5,
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": "-1",
                "length": "1",
            },
            "offset must be",
        ),
        (
            {
                "filename": "example.org-2020-001.warc.gz",
                "offset": "0",
                "length": "0",
            },
            "length must be",
        ),
        (
            {
                "filename": "example.org-2020-001.warc.gz",
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
        validate(collection)

    assert str(index) in str(raised.value)
    assert "line 1" in str(raised.value)
    assert "example.org" in str(raised.value)


@pytest.mark.parametrize(
    "filename",
    (
        "/example.org-2020-001.warc.gz",
        "../example.org-2020-001.warc.gz",
        "archive/fixture.warc.gz",
        "archive/../fixture.warc.gz",
        "archive//fixture.warc.gz",
        r"archive\fixture.warc.gz",
        "C:/example.org-2020-001.warc.gz",
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
        validate(collection)


def test_escaping_warc_symlink_is_rejected(collection_factory, tmp_path):
    root, collection, index, warc = collection_factory()
    outside = tmp_path / "outside.warc.gz"
    outside.write_bytes(b"x" * 128)
    warc.unlink()
    warc.symlink_to(outside)

    with pytest.raises(ValidationError, match="escapes or cannot be resolved"):
        validate(collection)


def test_escaping_index_symlink_is_rejected(collection_factory, tmp_path):
    _, collection, index, _ = collection_factory()
    outside = tmp_path / "outside.cdxj"
    index.replace(outside)
    index.symlink_to(outside)

    with pytest.raises(ValidationError, match="index escapes"):
        validate(collection)


def test_unexpected_second_index_is_rejected(collection_factory):
    _, collection, index, _ = collection_factory()
    (index.parent / "foreign.cdxj").write_text("x\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="unexpected indexes"):
        validate(collection)


def test_foreign_collection_warc_basename_is_rejected(collection_factory):
    payload = {
        "filename": "example.org-2021-001.warc.gz",
        "offset": "0",
        "length": "1",
    }
    _, collection, _, _ = collection_factory(
        entries=[("org,example)/", "20200101000000", payload)]
    )

    with pytest.raises(ValidationError, match="foreign or invalid"):
        validate(collection)


def test_index_must_be_nonempty_valid_utf8_and_sorted(collection_factory):
    _, collection, index, _ = collection_factory()
    index.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="index is empty"):
        validate(collection)

    index.write_bytes(b"\xff")
    with pytest.raises(ValidationError, match="valid UTF-8"):
        validate(collection)

    payload = {
        "filename": "example.org-2020-001.warc.gz",
        "offset": "0",
        "length": "1",
    }
    index.write_text(
        f"org,z)/ 20200101000000 {json.dumps(payload)}\n"
        f"org,a)/ 20200101000000 {json.dumps(payload)}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="not sorted"):
        validate(collection)


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
            validation.validate_collection(
                selected(collection), archive_id="example.org"
            )


def test_each_distinct_warc_is_validated_once(
    collection_factory,
    monkeypatch,
):
    entries = [
        (
            "org,example)/",
            f"20200{number}01000000",
            {
                "filename": "example.org-2020-001.warc.gz",
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

    validate(collection)

    assert calls == ["example.org-2020-001.warc.gz"]
