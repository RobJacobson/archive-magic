"""Acquire complete calendar-year CC queries, cached independently per crawl."""

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex, catalog_timestamp, validate_collection
from archive_magic_fetch.archive.identity import make_identity, normalize_payload_digest
from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search
from archive_magic_fetch.contracts import DiscoveryRequest
from archive_magic_fetch.models import CaptureListing, CaptureRef, CommonCrawlLocator
from archive_magic_fetch.runtime.manage_archive_files import exclusive_temp_path, publish_file_atomically


from archive_magic_fetch.pipeline.publication.storage import completed_discovery

PAGE_SIZE = 5


def load_or_fetch_common_crawl_year(
    request: DiscoveryRequest, *, index_directory: Path, index: CommonCrawlIndex, client,
) -> CaptureListing:
    search_url, match_type = normalize_cdx_search(request.url_pattern)
    start, end = f"{request.year}0101000000", f"{request.year}1231235959"
    query = {"url": search_url}
    if match_type is not None:
        query["matchType"] = match_type
    query_hash = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    captures = []
    coverage = []
    for collection in index.catalog(client):
        if catalog_timestamp(collection["from"]) > end or catalog_timestamp(collection["to"]) < start:
            continue
        coverage.append(collection["id"])
        path = index_directory / "common-crawl" / "v1" / query_hash / collection["id"] / f"{request.year}.json"
        metadata = {"version": 1, "query": query, "from": start, "to": end, "collection": collection}
        cached = _load_cache(path, metadata)
        if cached is not None:
            completed_discovery(path)
            captures.extend(cached)
            continue
        params = {**query, "from": start, "to": end, "output": "json", "pageSize": PAGE_SIZE}
        counts = index.read(client, collection["cdx-api"], {**params, "showNumPages": "true"}, allow_empty=True)
        if counts is None:
            pages = 0
        else:
            value = json.loads(counts)
            pages = value.get("pages") if isinstance(value, dict) else None
            if type(pages) is not int or pages < 0:
                raise ValueError("invalid Common Crawl page count")
        rows = []
        for page in range(pages):
            # Page counts precede date filtering. A numbered page can therefore
            # return the recognized no-captures response for this calendar year.
            text = index.read(
                client, collection["cdx-api"], {**params, "page": page}, allow_empty=True,
            )
            if text is None:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                capture = _parse_row(json.loads(line), collection["id"])
                if start <= capture.identity.timestamp <= end:
                    rows.append(capture)
        _save_cache(path, {**metadata, "captures": [asdict(c) for c in rows]})
        completed_discovery(path)
        captures.extend(rows)
    return CaptureListing(tuple(captures), {"source": "common-crawl", **query, "from": start, "to": end, "collections": coverage})


def _parse_row(row, crawl_id: str) -> CaptureRef:
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
    if (not isinstance(status, str) or not isinstance(digest, str)
            or (status != "-" and (len(status) != 3 or not status.isascii() or not status.isdigit()
                                  or not 100 <= int(status) <= 599))
            or (digest != "-" and normalize_payload_digest(digest) is None)):
        raise ValueError("invalid Common Crawl status/digest")
    return CaptureRef(
        make_identity(original_url=row["url"], timestamp=timestamp, status_token=status,
                      payload_digest=digest, urlkey=row["urlkey"]),
        row["mime"],
        CommonCrawlLocator(crawl_id, row["filename"], _integer(row.get("offset")), _integer(row.get("length"))),
    )


def _integer(value) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("invalid Common Crawl offset/length")


def _load_cache(path: Path, metadata: dict) -> tuple[CaptureRef, ...] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {*metadata, "captures"}:
            raise ValueError("invalid cache envelope")
        if any(value[key] != metadata[key] for key in ("version", "query", "from", "to")):
            raise ValueError("cache scope/version mismatch")
        if not isinstance(value["captures"], list):
            raise ValueError("invalid cached captures")
        captures = []
        for entry in value["captures"]:
            identity, locator = entry["identity"], entry["locator"]
            capture = _parse_row({
                "urlkey": identity["urlkey"], "url": identity["original_url"],
                "timestamp": identity["timestamp"], "status": identity["status_token"],
                "digest": identity["payload_digest"], "mime": entry["mime"],
                "filename": locator["filename"], "offset": locator["offset"], "length": locator["length"],
            }, locator["crawl_id"])
            if (locator["crawl_id"] != metadata["collection"]["id"]
                    or not metadata["from"] <= capture.identity.timestamp <= metadata["to"]):
                raise ValueError("cached capture outside scope")
            captures.append(capture)
        if not isinstance(value["collection"], dict) or set(value["collection"]) != set(metadata["collection"]):
            raise ValueError("invalid cached collection metadata")
        validate_collection(value["collection"])
        if value["collection"]["id"] != metadata["collection"]["id"]:
            raise ValueError("cached collection ID mismatch")
        if value["collection"] != metadata["collection"]:
            return None
        return tuple(captures)
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise ValueError(f"invalid Common Crawl cache {path}: {error}") from error


def _save_cache(path: Path, value: dict) -> None:
    temp = exclusive_temp_path(path.parent, suffix=".json.tmp")
    try:
        temp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        publish_file_atomically(temp, path)
    finally:
        temp.unlink(missing_ok=True)
