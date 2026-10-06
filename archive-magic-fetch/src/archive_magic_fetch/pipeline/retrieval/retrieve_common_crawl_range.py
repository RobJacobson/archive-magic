"""Retrieve exactly one advertised compressed Common Crawl record range."""

import re
from concurrent.futures import CancelledError

from archive_magic_fetch.adapters.interpret_common_crawl_failures import CorruptRecord, RecordLimitExceeded
from archive_magic_fetch.models import CaptureRef


_DATA_ROOT = "https://data.commoncrawl.org/"
MAX_COMPRESSED_RECORD_BYTES = 32 * 1024 * 1024


def retrieve_common_crawl_range(client, capture: CaptureRef) -> bytes:
    locator = capture.locator
    if locator is None:
        raise ValueError("Common Crawl capture has no locator")
    if locator.length > MAX_COMPRESSED_RECORD_BYTES:
        raise RecordLimitExceeded("archive record exceeds compressed size limit")
    end = locator.offset + locator.length - 1
    # Do not follow redirects: even a redirect response body could be unbounded.
    with client.get(
        _DATA_ROOT + locator.filename,
        headers={"Range": f"bytes={locator.offset}-{end}", "Accept-Encoding": "identity"},
        stream=True, allow_redirects=False, timeout=(10, 60),
    ) as response:
        response.raise_for_status()
        if response.status_code != 206:
            raise CorruptRecord(f"range request returned HTTP {response.status_code}, expected 206")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
        if (not match or int(match[1]) != locator.offset or int(match[2]) != end
                or (match[3] != "*" and int(match[3]) <= end)):
            raise CorruptRecord("invalid Content-Range")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise CorruptRecord("unexpected outer Content-Encoding")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or int(length) != locator.length):
            raise CorruptRecord("invalid range Content-Length")
        chunks = []
        remaining = locator.length + 1
        try:
            while remaining:
                chunk = response.raw.read(min(65536, remaining), decode_content=False)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except CancelledError:
            raise
        except Exception as error:
            raise CorruptRecord(f"incomplete range body: {error}") from error
        data = b"".join(chunks)
        if len(data) != locator.length:
            raise CorruptRecord(f"range length mismatch: expected {locator.length}, got {len(data)}")
        return data
