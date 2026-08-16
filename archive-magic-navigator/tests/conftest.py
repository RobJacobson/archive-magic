from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def collection_factory(tmp_path):
    def create(
        archive_id="example.org",
        *,
        collection_id="2020",
        entries=None,
        warc_size=128,
    ):
        root = tmp_path / "archives"
        archive = root / archive_id
        collection = archive
        warc_name = f"{archive_id}-{collection_id}-001.warc.gz"
        warc = collection / warc_name
        warc.parent.mkdir(parents=True, exist_ok=True)
        warc.write_bytes(b"x" * warc_size)
        if entries is None:
            entries = [
                (
                    "org,example)/",
                    "20200101000000",
                    {
                        "filename": warc_name,
                        "offset": "0",
                        "length": "16",
                    },
                )
            ]
        index = collection / f"{archive_id}-{collection_id}-index.cdxj"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            "".join(
                f"{key} {timestamp} {json.dumps(payload)}\n"
                for key, timestamp, payload in entries
            ),
            encoding="utf-8",
        )
        return root, archive, index, warc

    return create
