"""Build a durable operator report from stored historical redirects."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit

from warcio.archiveiterator import ArchiveIterator

from .capture_identity import normalized_urlkey
from .collection_paths import normalize_domain


REDIRECT_REPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RedirectReport:
    """Published redirect report and its operator-facing counts."""

    path: Path
    skipped: int
    covered: int
    unresolved: int


def _header(headers, name: str) -> Optional[str]:
    if headers is None:
        return None
    expected = name.lower()
    for header_name, value in headers.headers:
        if header_name.lower() == expected and str(value).strip():
            return str(value).strip()
    return None


def _normalized_target(base_url: str, location: str) -> tuple[str, str]:
    resolved = urljoin(base_url, location)
    parsed = urlsplit(resolved)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"unsupported redirect scheme: {parsed.scheme or '-'}")
    host, port = normalize_domain(resolved)
    authority_host = f"[{host}]" if ":" in host else host
    authority = authority_host if port is None else f"{authority_host}:{port}"
    target = urlunsplit(
        (scheme, authority, parsed.path or "/", parsed.query, "")
    )
    return target, authority


def _timestamp(record) -> str:
    value = record.rec_headers.get_header("WARC-Date")
    if not value:
        raise ValueError("redirect record has no WARC-Date")
    return value


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".redirects-",
        suffix=".json.tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_redirect_report(warcs: Sequence[Path]) -> dict[str, object]:
    """Return a full-collection redirect report payload."""

    covered_urlkeys: set[str] = set()
    occurrences: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []

    for path in warcs:
        with path.open("rb") as stream:
            iterator = ArchiveIterator(stream, check_digests="raise")
            for record in iterator:
                if record.rec_type not in {"response", "revisit"}:
                    record.raw_stream.read()
                    continue
                source_url = record.rec_headers.get_header("WARC-Target-URI")
                if not source_url:
                    raise ValueError(f"{path} contains a record without a target URI")
                covered_urlkeys.add(normalized_urlkey(source_url))
                status_text = (
                    record.http_headers.get_statuscode()
                    if record.http_headers is not None
                    else None
                )
                if status_text is None or not status_text.isdigit():
                    raise ValueError(f"{path} contains a record without a numeric status")
                status = int(status_text)
                if not (300 <= status < 400) or status == 304:
                    record.raw_stream.read()
                    continue
                timestamp = _timestamp(record)
                location = _header(record.http_headers, "Location")
                if location is None:
                    unresolved.append(
                        {
                            "capture_timestamp": timestamp,
                            "reason": "missing Location header",
                            "source_url": source_url,
                            "status": status,
                        }
                    )
                    record.raw_stream.read()
                    continue
                try:
                    target_url, target_site = _normalized_target(
                        source_url,
                        location,
                    )
                except ValueError as error:
                    unresolved.append(
                        {
                            "capture_timestamp": timestamp,
                            "location": location,
                            "reason": str(error),
                            "source_url": source_url,
                            "status": status,
                        }
                    )
                else:
                    occurrences.append(
                        {
                            "capture_timestamp": timestamp,
                            "source_url": source_url,
                            "status": status,
                            "target_site": target_site,
                            "target_url": target_url,
                        }
                    )
                record.raw_stream.read()
            if iterator.err_count:
                raise ValueError(
                    f"{path} contains {iterator.err_count} malformed record "
                    "boundary warning(s)"
                )

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for occurrence in occurrences:
        grouped[str(occurrence["target_url"])].append(occurrence)

    targets = []
    summary = Counter()
    for target_url in sorted(grouped):
        items = grouped[target_url]
        classification = (
            "covered"
            if normalized_urlkey(target_url) in covered_urlkeys
            else "skipped"
        )
        summary[classification] += 1
        source_counts = Counter(
            (
                str(item["source_url"]),
                int(item["status"]),
                str(item["capture_timestamp"]),
            )
            for item in items
        )
        sources = [
            {
                "capture_timestamp": timestamp,
                "occurrence_count": count,
                "source_url": source_url,
                "status": status,
            }
            for (source_url, status, timestamp), count in sorted(
                source_counts.items()
            )
        ]
        targets.append(
            {
                "classification": classification,
                "occurrence_count": len(items),
                "sources": sources,
                "target_site": items[0]["target_site"],
                "target_url": target_url,
            }
        )

    unresolved.sort(
        key=lambda item: (
            str(item["source_url"]),
            str(item["capture_timestamp"]),
            int(item["status"]),
        )
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "schema_version": REDIRECT_REPORT_SCHEMA_VERSION,
        "summary": {
            "covered_targets": summary["covered"],
            "redirect_occurrences": len(occurrences),
            "skipped_targets": summary["skipped"],
            "unresolved_occurrences": len(unresolved),
        },
        "targets": targets,
        "unresolved": unresolved,
    }


def write_redirect_report(
    warcs: Sequence[Path],
    path: Path,
) -> RedirectReport:
    """Build and atomically publish one full-collection redirect report."""

    payload = build_redirect_report(warcs)
    _atomic_json(path, payload)
    summary = payload["summary"]
    return RedirectReport(
        path=path,
        skipped=int(summary["skipped_targets"]),
        covered=int(summary["covered_targets"]),
        unresolved=int(summary["unresolved_occurrences"]),
    )
