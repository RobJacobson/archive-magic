"""Discovery cache envelopes and managed-path grammar."""

import json
from dataclasses import dataclass

import pytest

from archive_magic_fetch.config.models import FetchOutput
from archive_magic_fetch.models import RunMetrics
from archive_magic_fetch.pipeline.discovery.cache import (
    common_crawl_cache_relative,
    is_managed_discovery_path,
    validate_cache,
)
from archive_magic_fetch.pipeline.discovery.load_or_fetch_common_crawl_year import _load_cache
from archive_magic_fetch.pipeline.publication.storage import BucketStorage
from archive_magic_fetch.pipeline.run_fetch import _accumulate_metrics
from common_crawl_helpers import collection


def _year_document(query, crawl=None):
    return {
        "version": 1,
        "query": query,
        "from": "20170101000000",
        "to": "20171231235959",
        "collection": crawl or collection(),
        "captures": [],
    }


def _write(tmp_path, relative, document):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


def test_null_query_fails_publication_validation(tmp_path):
    document = _year_document(None)
    relative = common_crawl_cache_relative({"url": "https://example.org/*"}, "CC-MAIN-2017-26", 2017)
    # Place the invalid document on a grammatical path. Validation must read the
    # envelope instead of accepting it because the path looks managed.
    path = _write(tmp_path, relative, document)
    with pytest.raises(ValueError, match="invalid discovery query"):
        validate_cache(path, relative)


def test_partial_year_bounds_fail_validation(tmp_path):
    document = _year_document({"url": "https://example.org/*", "matchType": "domain"})
    document["from"] = "20170601000000"
    relative = common_crawl_cache_relative(document["query"], document["collection"]["id"], 2017)
    path = _write(tmp_path, relative, document)
    with pytest.raises(ValueError, match="full calendar year"):
        validate_cache(path, relative)


def test_valid_empty_cache_matches_its_constructed_path(tmp_path):
    query = {"url": "https://example.org/*", "matchType": "domain"}
    document = _year_document(query)
    relative = common_crawl_cache_relative(query, document["collection"]["id"], 2017)
    path = _write(tmp_path, relative, document)
    validate_cache(path, relative)
    assert is_managed_discovery_path("discovery/" + relative)


def test_provenance_rejects_a_grammatical_path_for_another_query(tmp_path):
    query = {"url": "https://example.org/*", "matchType": "domain"}
    document = _year_document(query)
    stored = common_crawl_cache_relative(query, document["collection"]["id"], 2017)
    other = common_crawl_cache_relative({"url": "https://other.example/*"}, "CC-MAIN-2017-26", 2017)
    path = _write(tmp_path, other, document)
    with pytest.raises(ValueError, match="provenance"):
        validate_cache(path, other)
    assert stored != other


def test_managed_path_requires_a_common_crawl_id(tmp_path):
    digest = "ab" * 32
    real = f"discovery/common-crawl/v1/{digest}/CC-MAIN-2017-26/2017.json"
    foreign = f"discovery/common-crawl/v1/{digest}/not-a-crawl/2017.json"
    assert is_managed_discovery_path(real)
    assert not is_managed_discovery_path(foreign)

    root = tmp_path / "output"
    for name in ("data", "discovery", "logs", ".state"):
        (root / name).mkdir(parents=True)
    store = BucketStorage(
        FetchOutput("remote", root / "data", "bucket", "prefix"),
        "example.org",
        client=object(),
    )
    assert store.managed(real)
    assert not store.managed(foreign)


def test_changed_collection_metadata_is_reread_not_rejected(tmp_path):
    query = {"url": "https://example.org/*", "matchType": "domain"}
    document = _year_document(query)
    path = _write(tmp_path, "cache.json", document)
    metadata = {
        "version": 1,
        "query": query,
        "from": document["from"],
        "to": document["to"],
        "collection": {**document["collection"], "to": "2018-01-01T00:00:00"},
    }
    assert _load_cache(path, metadata) is None


def test_metric_totals_sum_numbers_and_reject_other_types():
    total = RunMetrics()
    current = RunMetrics(downloads=2, source_recovered=1, revisits=3)
    current.bump_attempt("retry_exhausted")
    _accumulate_metrics(total, current)
    assert total.downloads == 2
    assert total.source_recovered == 1
    assert total.revisits == 3
    assert total.attempts_by_category == {"retry_exhausted": 1}
    assert not hasattr(total, "payload_reuses")

    @dataclass
    class Sample:
        count: int = 0
        label: str = ""

    with pytest.raises(TypeError, match="label"):
        _accumulate_metrics(Sample(), Sample(label="later"))
