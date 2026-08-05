"""Tests for collection coverage merge and bootstrap."""

from __future__ import annotations

import pytest

from archive_magic_fetch import collection_coverage as coverage
from archive_magic_fetch import collection_paths


def layout(tmp_path):
    return collection_paths.collection_paths(
        "example.com/*",
        root=tmp_path / "archives",
    )


def test_save_and_load_coverage_round_trip(tmp_path):
    paths = layout(tmp_path)
    written = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        files_mode="none",
    )
    path = coverage.save_coverage(paths, written)
    assert path == paths.coverage_path
    loaded = coverage.load_coverage(paths)
    assert loaded == written


def test_merge_expands_to_union_window():
    prior = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        files_mode="none",
    )
    window = coverage.merge_search_window(
        url_pattern="example.com/*",
        date_start="2005",
        date_end="2010",
        files_mode="none",
        prior=prior,
    )
    assert window.date_start == "1995"
    assert window.date_end == "2010"
    assert window.expanded is True


def test_old_coverage_schema_is_rejected():
    with pytest.raises(ValueError, match="unsupported coverage schema_version"):
        coverage.CollectionCoverage.from_dict(
            {
                "schema_version": 1,
                "url_pattern": "example.com/*",
                "date_start": "1995",
                "date_end": "2005",
                "warc_mode": "all",
                "files_mode": "none",
                "redirect_capture": "page",
            }
        )


def test_coverage_without_schema_is_rejected():
    with pytest.raises(ValueError, match="coverage missing field: schema_version"):
        coverage.CollectionCoverage.from_dict(
            {
                "url_pattern": "example.com/*",
                "date_start": "1995",
                "date_end": "2005",
                "files_mode": "none",
            }
        )


def test_date_padding_chooses_more_inclusive_bounds():
    assert coverage.earlier_date("1995", "19950101") == "1995"
    assert coverage.later_date("2000", "20051231") == "20051231"
