import hashlib
import os
from pathlib import Path

import pytest

from archive_magic_fetch import paths


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def capture_groups(url="https://example.com/", urlkey="com,example)/"):
    return {urlkey: [{"url": url}]}


def test_root_url_has_recognizable_deterministic_path():
    url = "https://example.com/"

    assert paths.warc_path(url) == Path(
        f"../archives/https/example.com/index--{url_hash(url)}.warc.gz"
    )


def test_nested_url_uses_final_segment_and_hashes_query():
    url = "https://example.com/images/logo.png?v=2"

    assert paths.warc_path(url) == Path(
        f"../archives/https/example.com/images/logo.png--{url_hash(url)}.warc.gz"
    )


def test_query_and_scheme_variants_have_distinct_paths():
    urls = [
        "https://example.com/image.png?v=1",
        "https://example.com/image.png?v=2",
        "http://example.com/image.png?v=1",
    ]

    assert len({paths.warc_path(url) for url in urls}) == 3


def test_group_path_is_stable_and_hashes_urlkey(tmp_path):
    urlkey = "com,example)/index.html"

    result = paths.preflight_paths(
        capture_groups(urlkey=urlkey), root=tmp_path / "archives"
    )[urlkey]

    assert result == (
        tmp_path
        / "archives"
        / "urlkey"
        / "com%2Cexample%29"
        / f"index.html--{url_hash(urlkey)}.warc.gz"
    )


def test_port_is_included_and_userinfo_is_excluded():
    url = "https://user:secret@example.com:8443/a"
    result = paths.warc_path(url)
    relative = result.relative_to(paths.DEFAULT_OUTPUT_ROOT)

    assert relative.parts[:2] == ("https", "example.com%3A8443")
    assert "user" not in str(result)
    assert "secret" not in str(result)


def test_unsafe_empty_and_dot_segments_are_single_safe_components():
    url = "https://example.com/a//../CON./file:name"
    result = paths.warc_path(url)
    relative = result.relative_to(paths.DEFAULT_OUTPUT_ROOT)

    assert relative.parts[2:6] == ("a", "%00", "%2E%2E", "CON%2E")
    assert result.name.startswith("file%3Aname--")
    assert result.is_relative_to(Path("../archives"))


@pytest.mark.parametrize(
    "url",
    ["example.com/path", "/relative/path", "https:///missing-host"],
)
def test_discovered_capture_url_requires_scheme_and_host(url):
    with pytest.raises(ValueError, match="scheme and host"):
        paths.warc_path(url)


def test_preflight_rejects_existing_target(tmp_path):
    url = "https://example.com/"
    urlkey = "com,example)/"
    target = paths.urlkey_warc_path(urlkey, root=tmp_path / "archives")
    target.parent.mkdir(parents=True)
    target.touch()

    with pytest.raises(FileExistsError, match="already exists"):
        paths.preflight_paths(
            capture_groups(url=url, urlkey=urlkey),
            root=tmp_path / "archives",
        )


def test_preflight_rejects_broken_symlink_target(tmp_path):
    url = "https://example.com/"
    urlkey = "com,example)/"
    target = paths.urlkey_warc_path(urlkey, root=tmp_path / "archives")
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing")

    with pytest.raises(FileExistsError, match="already exists"):
        paths.preflight_paths(
            capture_groups(url=url, urlkey=urlkey),
            root=tmp_path / "archives",
        )


def test_preflight_rejects_generated_collision_before_retrieval(tmp_path, monkeypatch):
    collision = tmp_path / "archives" / "same.warc.gz"
    monkeypatch.setattr(
        paths, "urlkey_warc_path", lambda urlkey, root: collision
    )

    with pytest.raises(ValueError, match="collision"):
        paths.preflight_paths(
            {
                "com,example)/a": [{"url": "https://example.com/a"}],
                "com,example)/b": [{"url": "https://example.com/b"}],
            },
            root=tmp_path / "archives",
        )


def test_preflight_allows_symlinked_output_ancestor(tmp_path):
    real_root = tmp_path / "real-archives"
    real_root.mkdir()
    linked_root = tmp_path / "archives"
    linked_root.symlink_to(real_root, target_is_directory=True)

    result = paths.preflight_paths(
        capture_groups(), root=linked_root
    )

    assert result["com,example)/"].is_relative_to(linked_root)


def test_preflight_reports_non_directory_ancestor(tmp_path):
    root = tmp_path / "archives"
    root.write_text("not a directory")

    with pytest.raises(OSError, match="cannot inspect output path"):
        paths.preflight_paths(capture_groups(), root=root)
