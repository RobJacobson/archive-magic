from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def collection_factory(tmp_path):
    def create(
        collection_id="example.org",
        *,
        entries=None,
        warc_size=128,
    ):
        root = tmp_path / "archives"
        collection = root / collection_id
        warc = collection / "archive" / "example.org" / "index.warc.gz"
        warc.parent.mkdir(parents=True, exist_ok=True)
        warc.write_bytes(b"x" * warc_size)
        if entries is None:
            entries = [
                (
                    "org,example)/",
                    "20200101000000",
                    {
                        "filename": "archive/example.org/index.warc.gz",
                        "offset": "0",
                        "length": "16",
                    },
                )
            ]
        index = collection / "index.cdxj"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            "".join(
                f"{key} {timestamp} {json.dumps(payload)}\n"
                for key, timestamp, payload in entries
            ),
            encoding="utf-8",
        )
        return root, collection, index, warc

    return create
