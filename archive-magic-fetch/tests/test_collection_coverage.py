"""Tests for collection coverage merge and bootstrap."""

from __future__ import annotations

import json

import pytest

from archive_magic_fetch import collection_coverage as coverage
from archive_magic_fetch import collection_paths


def layout(tmp_path):
    return collection_paths.collection_paths(
        "example.com/*",
        root=tmp_path / "archives",
    )


def write_source_query(
    paths: collection_paths.CollectionPaths,
    *,
    identifier: str,
    url_pattern: str = "example.com/*",
    date_start: str,
    date_end: str,
) -> None:
    directory = paths.sources_root / identifier
    directory.mkdir(parents=True)
    (directory / "query.json").write_text(
        json.dumps(
            {
                "url_pattern": url_pattern,
                "date_start": date_start,
                "date_end": date_end,
            }
        ),
        encoding="utf-8",
    )


def test_save_and_load_coverage_round_trip(tmp_path):
    paths = layout(tmp_path)
    written = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    path = coverage.save_coverage(paths, written)
    assert path == paths.coverage_path
    loaded = coverage.load_coverage(paths)
    assert loaded == written


def test_bootstrap_from_source_queries_min_max(tmp_path):
    paths = layout(tmp_path)
    write_source_query(
        paths,
        identifier="a",
        date_start="2000",
        date_end="2005",
    )
    write_source_query(
        paths,
        identifier="b",
        date_start="1995",
        date_end="2003",
    )
    write_source_query(
        paths,
        identifier="other",
        url_pattern="other.com/*",
        date_start="1900",
        date_end="2099",
    )

    bootstrapped = coverage.bootstrap_coverage(
        paths,
        url_pattern="example.com/*",
    )
    assert bootstrapped is not None
    assert bootstrapped.date_start == "1995"
    assert bootstrapped.date_end == "2005"
    assert bootstrapped.modes_confirmed is False


def test_merge_expands_to_union_window():
    prior = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    window = coverage.merge_search_window(
        url_pattern="example.com/*",
        date_start="2005",
        date_end="2010",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
        prior=prior,
        fresh=False,
    )
    assert window.date_start == "1995"
    assert window.date_end == "2010"
    assert window.expanded is True


def test_merge_fresh_ignores_prior():
    prior = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    window = coverage.merge_search_window(
        url_pattern="example.com/*",
        date_start="2005",
        date_end="2010",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
        prior=prior,
        fresh=True,
    )
    assert window.date_start == "2005"
    assert window.date_end == "2010"
    assert window.expanded is False


def test_merge_rejects_warc_mode_mismatch():
    prior = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="none",
    )
    with pytest.raises(coverage.CoverageModeError, match="warc_mode"):
        coverage.merge_search_window(
            url_pattern="example.com/*",
            date_start="2005",
            date_end="2010",
            warc_mode="latest",
            files_mode="none",
            redirect_capture="none",
            prior=prior,
            fresh=False,
        )


def test_bootstrapped_coverage_skips_mode_checks():
    prior = coverage.CollectionCoverage(
        url_pattern="example.com/*",
        date_start="1995",
        date_end="2005",
        warc_mode="all",
        files_mode="none",
        redirect_capture="page",
        modes_confirmed=False,
    )
    window = coverage.merge_search_window(
        url_pattern="example.com/*",
        date_start="2005",
        date_end="2010",
        warc_mode="latest",
        files_mode="unique",
        redirect_capture="none",
        prior=prior,
        fresh=False,
    )
    assert window.date_start == "1995"
    assert window.date_end == "2010"


def test_date_padding_chooses_more_inclusive_bounds():
    assert coverage.earlier_date("1995", "19950101") == "1995"
    assert coverage.later_date("2000", "20051231") == "20051231"
