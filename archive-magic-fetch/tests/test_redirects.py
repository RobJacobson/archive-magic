import json
from pathlib import Path

from archive_magic_fetch.downloads import DownloadedCapture
from archive_magic_fetch.redirects import (
    REDIRECT_REPORT_SCHEMA_VERSION,
    build_redirect_report,
    write_redirect_report,
)
from archive_magic_fetch.warc_records import open_new_warc


def write_warc(path: Path, records) -> None:
    stream, writer = open_new_warc(path)
    try:
        for record in records:
            writer.write_record(record)
    finally:
        stream.close()


def response(
    url,
    captured,
    status,
    *,
    location=None,
    body=b"body",
):
    headers = [("Content-Type", "text/plain")]
    if location is not None:
        headers.append(("Location", location))
    return DownloadedCapture(
        body=body,
        url=url,
        capture_date=captured,
        source_uri=f"https://web.archive.org/web/{captured}/{url}",
        status_code=status,
        headers=tuple(headers),
    ).to_warc_record(target_url=url)


def test_report_aggregates_and_classifies_full_collection_redirects(tmp_path):
    source = tmp_path / "source.warc.gz"
    covered = tmp_path / "covered.warc.gz"
    write_warc(
        source,
        [
            response(
                "https://source.test/a/start",
                "2020-01-01T00:00:00Z",
                301,
                location="../landing#fragment",
            ),
            response(
                "https://source.test/a/start",
                "2021-01-01T00:00:00Z",
                302,
                location="https://source.test/landing",
            ),
            response(
                "https://source.test/other",
                "2022-01-01T00:00:00Z",
                307,
                location="https://skipped.test/path#part",
            ),
        ],
    )
    write_warc(
        covered,
        [
            response(
                "https://source.test/landing",
                "2020-01-01T00:00:01Z",
                200,
            )
        ],
    )

    payload = build_redirect_report([source, covered])

    assert payload["schema_version"] == REDIRECT_REPORT_SCHEMA_VERSION
    assert payload["summary"] == {
        "covered_targets": 1,
        "redirect_occurrences": 3,
        "skipped_targets": 1,
        "unresolved_occurrences": 0,
    }
    targets = {entry["target_url"]: entry for entry in payload["targets"]}
    assert targets["https://source.test/landing"]["classification"] == "covered"
    assert targets["https://source.test/landing"]["occurrence_count"] == 2
    assert targets["https://skipped.test/path"]["classification"] == "skipped"


def test_report_excludes_304_and_records_unresolved_redirects(tmp_path):
    warc = tmp_path / "redirects.warc.gz"
    write_warc(
        warc,
        [
            response("https://source.test/not-modified", "2020-01-01T00:00:00Z", 304),
            response("https://source.test/missing", "2020-01-02T00:00:00Z", 308),
            response(
                "https://source.test/invalid",
                "2020-01-03T00:00:00Z",
                305,
                location="mailto:person@example.com",
            ),
        ],
    )

    payload = build_redirect_report([warc])

    assert payload["summary"]["redirect_occurrences"] == 0
    assert payload["summary"]["unresolved_occurrences"] == 2
    assert {item["source_url"] for item in payload["unresolved"]} == {
        "https://source.test/missing",
        "https://source.test/invalid",
    }


def test_write_redirect_report_is_durable_json(tmp_path):
    warc = tmp_path / "redirects.warc.gz"
    write_warc(
        warc,
        [
            response(
                "https://source.test/",
                "2020-01-01T00:00:00Z",
                303,
                location="https://target.test/",
            )
        ],
    )
    destination = tmp_path / "sources" / "run" / "redirects.json"

    report = write_redirect_report([warc], destination)

    assert report.path == destination
    assert (report.skipped, report.covered, report.unresolved) == (1, 0, 0)
    assert json.loads(destination.read_text(encoding="utf-8"))["targets"][0][
        "target_site"
    ] == "target.test"
