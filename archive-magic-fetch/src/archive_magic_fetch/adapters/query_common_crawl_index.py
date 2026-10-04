"""Run-scoped Common Crawl catalog and sequential index request policy."""

import json
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from archive_magic_fetch.runtime.calculate_retry_delay import retry_after_from_error
from archive_magic_fetch.models import CommonCrawlLocator

CATALOG_URL = "https://index.commoncrawl.org/collinfo.json"


class CommonCrawlIndex:
    """Keep catalog coverage and pacing stable across one run's annual queries."""

    def __init__(self, *, sleep=time.sleep, clock=time.monotonic):
        self.sleep = sleep
        self.clock = clock
        self.next_start = 0.0
        self._catalog = None

    def catalog(self, client):
        if self._catalog is None:
            value = json.loads(self.read(client, CATALOG_URL))
            if not isinstance(value, list) or not value:
                raise ValueError("invalid Common Crawl catalog")
            collections = []
            seen = set()
            for entry in value:
                collection = validate_collection(entry)
                if collection["id"] in seen:
                    raise ValueError("duplicate Common Crawl collection")
                seen.add(collection["id"])
                collections.append(collection)
            self._catalog = tuple(sorted(collections, key=lambda c: (catalog_timestamp(c["from"]), c["id"])))
        return self._catalog

    def read(self, client, url, params=None, *, allow_empty=False):
        for attempt in range(1, 6):
            self.sleep(max(0, self.next_start - self.clock()))
            self.next_start = self.clock() + 1
            try:
                with client.get(url, params=params, timeout=(10, 120), allow_redirects=False) as response:
                    if allow_empty and response.status_code == 404:
                        # pywb uses 404 with this JSON message for absent captures.
                        try:
                            value = response.json()
                        except ValueError:
                            value = None
                        if (isinstance(value, dict) and isinstance(value.get("message"), str)
                                and value["message"].startswith("No Captures found for:")):
                            return None
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError(f"unexpected index HTTP {response.status_code}")
                    return response.text
            except (requests.RequestException,) as error:
                response = getattr(error, "response", None)
                status = getattr(response, "status_code", None)
                retryable = isinstance(error, (requests.Timeout, requests.ConnectionError,
                                              requests.exceptions.ChunkedEncodingError)) or status == 429 or (
                    status is not None and 500 <= status < 600
                )
                if not retryable or attempt == 5:
                    raise
                self.sleep(max(min(60 * 2 ** (attempt - 1), 600), retry_after_from_error(error) or 0))
        raise AssertionError("index retries exhausted")


def catalog_timestamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid catalog timestamp")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def validate_collection(entry: dict) -> dict[str, str]:
    """Validate catalog metadata before using it as cache coverage or an endpoint."""
    if not isinstance(entry, dict):
        raise ValueError("invalid Common Crawl collection")
    try:
        collection = {key: entry[key] for key in ("id", "cdx-api", "from", "to")}
        CommonCrawlLocator(collection["id"], "validation.warc.gz", 0, 1)
        endpoint = urlsplit(collection["cdx-api"])
        if (endpoint.scheme != "https" or endpoint.netloc != "index.commoncrawl.org"
                or endpoint.query or endpoint.fragment
                or endpoint.path != f"/{collection['id']}-index"):
            raise ValueError("unsafe Common Crawl index endpoint")
        start, end = (catalog_timestamp(collection[key]) for key in ("from", "to"))
        if start > end:
            raise ValueError("invalid Common Crawl collection bounds")
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError("invalid Common Crawl collection metadata") from error
    return collection
