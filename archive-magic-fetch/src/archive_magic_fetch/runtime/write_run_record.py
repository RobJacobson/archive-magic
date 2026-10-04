"""Run identifiers and structured completion records."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional, Sequence

from archive_magic_fetch.archive.format import parse_cdxj_line
from archive_magic_fetch.archive.identity import current_run_id, identity_to_dict
from archive_magic_fetch.archive.layout import ArchiveLayout
from archive_magic_fetch.models import (
    IndexArtifact,
    RunMetrics,
    UnresolvedFailure,
    WarcArtifact,
)
from archive_magic_fetch.runtime.manage_archive_files import (
    exclusive_temp_path,
    file_sha256,
    publish_file_atomically,
)


def init_run_id(layout: ArchiveLayout) -> str:
    """Allocate one invocation ID shared by every selected collection."""

    base = current_run_id()
    for attempt in range(1000):
        candidate = base if attempt == 0 else f"{base}-{attempt:02d}"
        if (
            not layout.run_record(candidate).exists()
            and not layout.run_log(candidate).exists()
        ):
            return candidate
    raise RuntimeError("unable to allocate a unique run source directory")


def init_run_record(layout: ArchiveLayout, run_id: str) -> Path:
    """Create the invocation's single structured log."""

    destination = layout.run_record(run_id)
    payload = {"run_id": run_id, "archive_id": layout.archive_id, "years": {}}
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + "\n")
    return destination


def write_run_record(
    layout: ArchiveLayout,
    *,
    collection_id: str,
    run_id: str,
    url_pattern: str,
    date_start: str,
    date_end: str,
    query: dict[str, object],
    warcs: Sequence[WarcArtifact],
    index: Optional[IndexArtifact],
    metrics: RunMetrics,
    failures: Sequence[UnresolvedFailure],
) -> Path:
    """Atomically publish the immutable completion record for one run slice."""

    ordered_failures = sorted(
        failures,
        key=lambda item: (item.identity.sort_key(), item.category.value, item.message),
    )
    year = {
        "collection_id": collection_id,
        "url_pattern": url_pattern,
        "date_start": date_start,
        "date_end": date_end,
        "query": query,
        "counts": {
            "selected": metrics.selected,
            "represented": metrics.represented,
            "locally_reused": metrics.local_reuses,
            "payload_reused": metrics.payload_reuses,
            "downloaded": metrics.downloads,
            "revisited": metrics.revisits,
            "digest_mismatch_accepted": metrics.digest_mismatch_accepted,
            "unresolved": metrics.unresolved,
        },
        "metrics": {
            "cdx_duration_s": round(metrics.cdx_duration_s, 3),
            "playback_attempts": metrics.playback_attempts,
            "playback_bytes": metrics.playback_bytes,
            "warc_write_s": round(metrics.warc_write_s, 3),
            "index_s": round(metrics.index_s, 3),
            "attempts_by_category": dict(sorted(metrics.attempts_by_category.items())),
        },
        "warcs": [
            {
                "filename": item.relative_key,
                "collection_id": item.collection_id,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "record_count": item.record_count,
            }
            for item in sorted(warcs, key=lambda w: w.relative_key)
        ],
        "index": (
            {
                "filename": index.relative_key,
                "size_bytes": index.size_bytes,
                "sha256": index.sha256,
                "capture_count": index.capture_count,
            }
            if index is not None
            else None
        ),
        "failures": [
            {
                "identity": identity_to_dict(item.identity),
                "category": item.category.value,
                "message": item.message,
            }
            for item in ordered_failures
        ],
    }
    destination = layout.run_record(run_id)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["years"][collection_id] = year
    tmp = exclusive_temp_path(destination.parent, suffix=".run.json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    publish_file_atomically(tmp, destination)
    return destination


def published_warc_artifacts(
    layout: ArchiveLayout,
    collection_id: str,
) -> list[WarcArtifact]:
    """Summarize committed WARCs from the CDXJ and size inventory."""

    index_path = layout.collection_index(collection_id)
    if not index_path.is_file():
        return []
    capture_counts: dict[str, int] = defaultdict(int)
    warc_names: set[str] = set()
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        try:
            filename = parse_cdxj_line(line)[2]["filename"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(filename, str):
            warc_names.add(filename)
            capture_counts[filename] += 1
    artifacts: list[WarcArtifact] = []
    for filename in sorted(warc_names):
        path = layout.root / filename
        size_bytes = path.stat().st_size
        sha256 = file_sha256(path)
        artifacts.append(
            WarcArtifact(
                relative_key=filename,
                collection_id=collection_id,
                sequence=int(
                    Path(filename).name.removesuffix(".warc.gz").rsplit("-", 1)[1]
                ),
                path=path,
                size_bytes=size_bytes,
                sha256=sha256,
                record_count=capture_counts[filename] + 1,
            )
        )
    return artifacts
