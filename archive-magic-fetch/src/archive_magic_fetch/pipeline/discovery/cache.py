"""Portable discovery cache provenance and validation."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from archive_magic_fetch.adapters.query_common_crawl_index import validate_collection
from archive_magic_fetch.archive.identity import (
    identity_from_dict,
    make_identity,
    normalize_payload_digest,
)
from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search
from archive_magic_fetch.models import COMMON_CRAWL_ID, CaptureRef, CommonCrawlLocator


_MATCH_TYPES = {"exact", "prefix", "host", "domain"}
_HASH = r"[0-9a-f]{64}"
_YEAR = r"[0-9]{4}"
_WAYBACK_NAME = re.compile(rf"wayback/v1/{_HASH}/{_YEAR}\.cdx\.json")
_COMMON_CRAWL_NAME = re.compile(
    rf"common-crawl/v1/{_HASH}/{COMMON_CRAWL_ID.pattern}/{_YEAR}\.json"
)
_YEAR_CACHE_KEYS = {"version", "query", "from", "to", "collection", "captures"}


def query_for(pattern):
    url, match = normalize_cdx_search(pattern)
    return {"url": url, **({"matchType": match} if match is not None else {})}


def query_hash(query):
    return hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()


def validate_discovery_query(query: object) -> dict:
    """Require a normalized url and optional match type, independent of any caller copy."""
    if (
        not isinstance(query, dict)
        or set(query) - {"url", "matchType"}
        or not isinstance(query.get("url"), str)
        or not query["url"]
        or ("matchType" in query and query["matchType"] not in _MATCH_TYPES)
    ):
        raise ValueError("invalid discovery query")
    return query


def calendar_year(start: object, end: object) -> int:
    """Return the year when both bounds are that year's complete UTC span."""
    if not isinstance(start, str) or not isinstance(end, str) or len(start) < 4 or not start[:4].isdigit():
        raise ValueError("discovery cache must cover a full calendar year")
    year = int(start[:4])
    if start != f"{year:04d}0101000000" or end != f"{year:04d}1231235959" or not 1 <= year <= 9999:
        raise ValueError("discovery cache must cover a full calendar year")
    return year


def wayback_cache_relative(query: dict, year: int) -> str:
    validate_discovery_query(query)
    if type(year) is not int or not 1 <= year <= 9999:
        raise ValueError("invalid Wayback discovery year")
    return f"wayback/v1/{query_hash(query)}/{year:04d}.cdx.json"


def common_crawl_cache_relative(query: dict, crawl_id: str, year: int) -> str:
    validate_discovery_query(query)
    if not isinstance(crawl_id, str) or not COMMON_CRAWL_ID.fullmatch(crawl_id):
        raise ValueError("invalid Common Crawl collection ID")
    if type(year) is not int or not 1 <= year <= 9999:
        raise ValueError("invalid Common Crawl discovery year")
    return f"common-crawl/v1/{query_hash(query)}/{crawl_id}/{year:04d}.json"


def discovery_cache_name(relative: str) -> bool:
    """True when a discovery-relative path matches a published cache name."""
    return bool(_WAYBACK_NAME.fullmatch(relative) or _COMMON_CRAWL_NAME.fullmatch(relative))


def is_managed_discovery_path(relative: str) -> bool:
    """True when an archive-relative key is a published discovery cache."""
    return relative.startswith("discovery/") and discovery_cache_name(relative.removeprefix("discovery/"))


def wayback_path(root, pattern, year):
    return Path(root) / wayback_cache_relative(query_for(pattern), year)


def wayback_document(pattern, year, captures):
    return {
        "version": 1,
        "source": "wayback",
        "query": query_for(pattern),
        "year": year,
        "captures": captures,
    }


def wayback_capture_from_dict(data: object) -> CaptureRef:
    fields = (
        "urlkey",
        "original_url",
        "timestamp",
        "status_token",
        "payload_digest",
        "mime",
    )
    if not isinstance(data, dict) or any(not isinstance(data.get(key), str) or not data[key] for key in fields):
        raise ValueError("capture fields must be non-empty strings")
    timestamp = data["timestamp"]
    if len(timestamp) != 14 or not timestamp.isascii() or not timestamp.isdigit():
        raise ValueError(f"invalid CDX timestamp: {timestamp!r}")
    datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    return CaptureRef(identity=identity_from_dict(data), mime=data["mime"])


def parse_common_crawl_index_row(row, crawl_id: str) -> CaptureRef:
    if not isinstance(row, dict):
        raise ValueError("invalid Common Crawl index row")
    for key in ("urlkey", "url", "timestamp", "mime", "filename"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"invalid Common Crawl {key}")
    timestamp = row["timestamp"]
    if len(timestamp) != 14 or not timestamp.isascii() or not timestamp.isdigit():
        raise ValueError("invalid Common Crawl timestamp")
    datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    status = row.get("status", "-")
    digest = row.get("digest", "-")
    if (
        not isinstance(status, str)
        or not isinstance(digest, str)
        or (status != "-" and (len(status) != 3 or not status.isascii() or not status.isdigit() or not 100 <= int(status) <= 599))
        or (digest != "-" and normalize_payload_digest(digest) is None)
    ):
        raise ValueError("invalid Common Crawl status/digest")
    return CaptureRef(
        make_identity(
            original_url=row["url"],
            timestamp=timestamp,
            status_token=status,
            payload_digest=digest,
            urlkey=row["urlkey"],
        ),
        row["mime"],
        CommonCrawlLocator(crawl_id, row["filename"], _integer(row.get("offset")), _integer(row.get("length"))),
    )


def parse_common_crawl_year_cache(value: object) -> tuple[CaptureRef, ...]:
    """Validate one published annual envelope. Catalog freshness is the fetcher's decision."""
    if not isinstance(value, dict) or set(value) != _YEAR_CACHE_KEYS:
        raise ValueError("invalid cache envelope")
    if value["version"] != 1:
        raise ValueError("invalid Common Crawl discovery metadata")
    validate_discovery_query(value["query"])
    calendar_year(value["from"], value["to"])
    collection = value["collection"]
    if not isinstance(collection, dict):
        raise ValueError("invalid cached collection metadata")
    validate_collection(collection)
    if not isinstance(value["captures"], list):
        raise ValueError("invalid cached captures")
    try:
        return tuple(
            cached_common_crawl_capture(
                entry, crawl_id=collection["id"], start=value["from"], end=value["to"],
            )
            for entry in value["captures"]
        )
    except (TypeError, KeyError) as error:
        raise ValueError("invalid cached capture") from error


def cached_common_crawl_capture(entry, *, crawl_id: str, start: str, end: str) -> CaptureRef:
    if not isinstance(entry, dict):
        raise ValueError("invalid cached capture")
    identity, locator = entry["identity"], entry["locator"]
    if not isinstance(identity, dict) or not isinstance(locator, dict):
        raise ValueError("invalid cached capture")
    capture = parse_common_crawl_index_row(
        {
            "urlkey": identity["urlkey"],
            "url": identity["original_url"],
            "timestamp": identity["timestamp"],
            "status": identity["status_token"],
            "digest": identity["payload_digest"],
            "mime": entry["mime"],
            "filename": locator["filename"],
            "offset": locator["offset"],
            "length": locator["length"],
        },
        locator["crawl_id"],
    )
    if locator["crawl_id"] != crawl_id or not start <= capture.identity.timestamp <= end:
        raise ValueError("cached capture outside scope")
    return capture


def validate_wayback(value, *, query=None, year=None):
    if not isinstance(value, dict) or set(value) != {"version", "source", "query", "year", "captures"}:
        raise ValueError("invalid Wayback cache envelope; migrate legacy discovery caches")
    if value["version"] != 1 or value["source"] != "wayback" or not isinstance(value["captures"], list):
        raise ValueError("invalid Wayback discovery metadata")
    validate_discovery_query(value["query"])
    actual_year = value["year"]
    if type(actual_year) is not int or not 1 <= actual_year <= 9999:
        raise ValueError("invalid Wayback discovery year")
    if query is not None and value["query"] != query or year is not None and actual_year != year:
        raise ValueError("Wayback cache query/year mismatch")
    captures = tuple(wayback_capture_from_dict(item) for item in value["captures"])
    if any(int(item.identity.timestamp[:4]) != actual_year for item in captures):
        raise ValueError("Wayback cache captures outside year")
    return captures


def validate_cache(path, relative):
    """Check envelope contents, then require the path to be the one those contents name."""
    value = json.loads(path.read_text())
    if not isinstance(relative, str) or not discovery_cache_name(relative):
        raise ValueError("unknown discovery cache namespace")
    if relative.startswith("wayback/"):
        validate_wayback(value)
        expected = wayback_cache_relative(value["query"], value["year"])
    else:
        parse_common_crawl_year_cache(value)
        expected = common_crawl_cache_relative(
            value["query"], value["collection"]["id"], calendar_year(value["from"], value["to"]),
        )
    if relative != expected:
        raise ValueError("discovery path/provenance mismatch")


def _integer(value) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("invalid Common Crawl offset/length")
