"""Acquire complete calendar-year CC queries, cached independently per crawl."""

import json
from dataclasses import asdict
from pathlib import Path

from archive_magic_fetch.adapters.query_common_crawl_index import CommonCrawlIndex, catalog_timestamp, validate_collection
from archive_magic_fetch.archive.normalize_cdx_search import normalize_cdx_search
from archive_magic_fetch.contracts import DiscoveryRequest
from archive_magic_fetch.models import CaptureListing, CaptureRef
from archive_magic_fetch.runtime.manage_archive_files import exclusive_temp_path, publish_file_atomically, write_json_durably
from archive_magic_fetch.runtime.report_progress import emit
from .cache import (
    cached_common_crawl_capture,
    calendar_year,
    common_crawl_cache_relative,
    parse_common_crawl_index_row,
    query_hash,
    validate_discovery_query,
)
from .checkpoints import progress_root, clear_progress

PAGE_SIZE = 5


def load_or_fetch_common_crawl_year(
    request: DiscoveryRequest, *, index_directory: Path, index: CommonCrawlIndex, client,
    checkpoint_directory: Path | None = None,
) -> CaptureListing:
    search_url, match_type = normalize_cdx_search(request.url_pattern)
    start, end = f"{request.year}0101000000", f"{request.year}1231235959"
    query = {"url": search_url}
    if match_type is not None:
        query["matchType"] = match_type
    digest = query_hash(query)
    captures = []
    coverage = []
    private_root = progress_root(index_directory, checkpoint_directory)
    for collection in index.catalog(client):
        if catalog_timestamp(collection["from"]) > end or catalog_timestamp(collection["to"]) < start:
            continue
        coverage.append(collection["id"])
        path = index_directory / common_crawl_cache_relative(query, collection["id"], request.year)
        progress = private_root / "common-crawl" / "v1" / digest / collection["id"] / str(request.year)
        metadata = {"version": 1, "query": query, "from": start, "to": end, "collection": collection}
        cached = _load_cache(path, metadata)
        if cached is not None:
            clear_progress(progress, private_root)
            if request.on_cache_complete is not None:
                request.on_cache_complete(path)
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
        unit = {**metadata, "pages": pages, "page_size": PAGE_SIZE}
        manifest = progress / "query.json"
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            if (not isinstance(saved, dict) or set(saved) != set(unit)
                    or any(saved[k] != unit[k] for k in ("version", "query", "from", "to"))
                    or type(saved["pages"]) is not int or saved["pages"] < 0
                    or type(saved["page_size"]) is not int or saved["page_size"] < 1):
                raise ValueError(f"invalid Common Crawl checkpoint: {manifest}")
            validate_collection(saved["collection"])
            if saved != unit:
                clear_progress(progress, private_root)
        write_json_durably(manifest, unit)
        rows = []
        for page in range(pages):
            page_path = progress / f"{page}.json"
            page_metadata = {**unit, "page": page}
            cached_page = _load_cache(page_path, page_metadata)
            if cached_page is not None:
                emit(f"using Common Crawl checkpoint: {collection['id']} year {request.year} page {page}")
                rows.extend(cached_page)
                continue
            # Page counts precede date filtering. A numbered page can therefore
            # return the recognized no-captures response for this calendar year.
            text = index.read(
                client, collection["cdx-api"], {**params, "page": page}, allow_empty=True,
            )
            page_rows = []
            for line in (text or "").splitlines():
                if not line.strip():
                    continue
                capture = parse_common_crawl_index_row(json.loads(line), collection["id"])
                if start <= capture.identity.timestamp <= end:
                    page_rows.append(capture)
            _save_cache(page_path, {**page_metadata, "captures": [asdict(c) for c in page_rows]})
            rows.extend(page_rows)
        _save_cache(path, {**metadata, "captures": [asdict(c) for c in rows]})
        clear_progress(progress, private_root)
        if request.on_cache_complete is not None:
            request.on_cache_complete(path)
        captures.extend(rows)
    return CaptureListing(tuple(captures), {"source": "common-crawl", **query, "from": start, "to": end, "collections": coverage})


def _load_cache(path: Path, metadata: dict) -> tuple[CaptureRef, ...] | None:
    """Return cached captures, or None when the file is absent or its catalog entry is stale.

    Query shape and calendar bounds are checked on the stored document. A changed
    collection record with the same id is a freshness miss, not a corrupt file.
    """
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {*metadata, "captures"}:
            raise ValueError("invalid cache envelope")
        if any(value[key] != metadata[key] for key in metadata if key != "collection"):
            raise ValueError("cache scope/version mismatch")
        validate_discovery_query(value["query"])
        calendar_year(value["from"], value["to"])
        if not isinstance(value["captures"], list):
            raise ValueError("invalid cached captures")
        captures = [
            cached_common_crawl_capture(
                entry,
                crawl_id=metadata["collection"]["id"],
                start=metadata["from"],
                end=metadata["to"],
            )
            for entry in value["captures"]
        ]
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
