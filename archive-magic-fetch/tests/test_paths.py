import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from wayback import CdxRecord

from archive_magic_fetch import paths


def _timestamp(value):
    return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )


def _capture(
    *,
    original="https://example.com/",
    captured="20060715085250",
    urlkey="com,example)/",
    digest="A" * 32,
):
    return CdxRecord(
        urlkey=urlkey,
        timestamp=_timestamp(captured),
        original=original,
        mimetype="text/html",
        statuscode=200,
        digest=digest,
        length=10,
    )


def layout(tmp_path, pattern="https://example.com/*"):
    return paths.collection_layout(pattern, root=tmp_path / "archives")


def groups(*urlkeys):
    return {urlkey: [object()] for urlkey in urlkeys}


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("https://Kevin.Burke.Dev/", "kevin.burke.dev"),
        ("http://www.example.com/*", "example.com"),
        ("*.example.com", "example.com"),
        ("https://example.com:443/*", "example.com"),
        ("http://example.com:80/*", "example.com"),
        ("https://example.com:8443/*", "example.com--port-8443"),
        ("example.com:443/*", "example.com--port-443"),
        ("https://münich.example/*", "xn--mnich-kva.example"),
        ("https://example.com./", "example.com"),
    ],
)
def test_collection_name_normalization(pattern, expected):
    assert paths.normalize_collection_name(pattern) == expected


def test_collection_idna_uses_the_python_codec(monkeypatch):
    monkeypatch.setitem(
        __import__("sys").modules,
        "idna",
        object(),
    )

    assert (
        paths.normalize_collection_name("https://münich.example/")
        == "xn--mnich-kva.example"
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "https://*.example.com/",
        "https:///missing-host",
        "https://user:secret@example.com/",
    ],
)
def test_collection_rejects_ambiguous_or_unsafe_patterns(pattern):
    with pytest.raises(ValueError):
        paths.normalize_collection_name(pattern)


@pytest.mark.parametrize(
    ("urlkey", "relative"),
    [
        ("com,example)/", "archive/index.warc.gz"),
        ("com,example)/about", "archive/about.warc.gz"),
        ("com,example)/posts", "archive/posts.warc.gz"),
        ("com,example)/posts/", "archive/posts/index.warc.gz"),
        (
            "com,example)/posts/hello-world",
            "archive/posts/hello-world.warc.gz",
        ),
        (
            "com,example)/images/logo.png",
            "archive/images/logo.png.warc.gz",
        ),
        (
            "com,example)/image.png?size=2",
            "archive/image.png%3Fsize%3D2.warc.gz",
        ),
        (
            "com,example)/?view=full",
            "archive/index%3Fview%3Dfull.warc.gz",
        ),
        (
            "com,example)/posts/?view=full",
            "archive/posts/index%3Fview%3Dfull.warc.gz",
        ),
    ],
)
def test_readable_urlkey_paths(tmp_path, urlkey, relative):
    collection = layout(tmp_path)

    result = paths.preferred_warc_path(urlkey, collection)

    assert result.relative_to(collection.collection_root) == Path(relative)
    assert "--" not in result.name


def test_unsafe_segments_cannot_reshape_the_collection(tmp_path):
    collection = layout(tmp_path)

    result = paths.preferred_warc_path(
        "com,example)/a//../CON./file:name",
        collection,
    )

    assert result.relative_to(collection.archive_root).parts == (
        "a",
        "%00",
        "%2E%2E",
        "CON%2E",
        "file%3Aname.warc.gz",
    )
    assert result.is_relative_to(collection.archive_root)


def test_overlong_component_is_bounded_without_hash(tmp_path):
    collection = layout(tmp_path)
    result = paths.preferred_warc_path(
        f"com,example)/{'x' * 400}",
        collection,
    )

    assert len(result.name.encode("ascii")) == paths.MAX_COMPONENT_BYTES
    assert result.name.endswith(".warc.gz")
    assert "--" not in result.name


def test_truncation_reencodes_an_exposed_trailing_dot(tmp_path):
    collection = layout(tmp_path)
    component = f"{'a' * 239}.tail"

    result = paths.preferred_warc_path(
        f"com,example)/{component}/resource",
        collection,
    )
    directory = result.relative_to(collection.archive_root).parts[0]

    assert len(directory.encode("ascii")) <= paths.MAX_COMPONENT_BYTES
    assert directory.endswith("%2E")
    assert not directory.endswith(".")


def test_truncation_reencodes_a_trailing_dot_run_within_limit(tmp_path):
    collection = layout(tmp_path)
    component = f"{'.' * paths.MAX_COMPONENT_BYTES}tail"

    result = paths.preferred_warc_path(
        f"com,example)/{component}/resource",
        collection,
    )
    directory = result.relative_to(collection.archive_root).parts[0]

    assert len(directory.encode("ascii")) <= paths.MAX_COMPONENT_BYTES
    assert directory.endswith("%2E")
    assert not directory.endswith(".")


def test_overlong_collection_name_is_rejected_instead_of_merged():
    label = "a" * 63
    host = ".".join([label, label, label, "a" * 50])

    with pytest.raises(ValueError, match="collection name exceeds"):
        paths.normalize_collection_name(f"https://{host}/")


def test_intentional_and_case_equivalent_paths_share_buckets(tmp_path):
    collection = layout(tmp_path)
    plan = paths.preflight_layout(
        groups(
            "com,example)/posts/",
            "com,example)/posts/index",
            "com,example)/Posts/index",
        ),
        collection,
    )

    assert plan.buckets == (
        paths.WarcBucket(
            collection.archive_root / "Posts" / "index.warc.gz",
            (
                "com,example)/Posts/index",
                "com,example)/posts/",
                "com,example)/posts/index",
            ),
        ),
    )


@pytest.mark.parametrize(
    ("parent", "descendant"),
    [
        (
            "com,example)/foo",
            "com,example)/foo.warc.gz/bar",
        ),
        (
            "com,example)/Foo",
            "com,example)/foo.warc.gz/bar",
        ),
        (
            f"com,example)/{'x' * 232}",
            f"com,example)/{'x' * 232}.warc.gzmore/bar",
        ),
    ],
)
def test_planned_file_directory_conflicts_share_ancestor_bucket(
    tmp_path,
    parent,
    descendant,
):
    collection = layout(tmp_path)

    plan = paths.preflight_layout(
        groups(descendant, parent),
        collection,
    )

    assert len(plan.buckets) == 1
    assert plan.buckets[0].path == paths.preferred_warc_path(
        parent,
        collection,
    )
    assert plan.buckets[0].urlkeys == tuple(sorted((parent, descendant)))


def test_bucket_spelling_and_order_ignore_discovery_order(tmp_path):
    first_layout = layout(tmp_path / "first")
    second_layout = layout(tmp_path / "second")
    keys = ("com,example)/b", "com,example)/A", "com,example)/a")

    first = paths.preflight_layout(groups(*keys), first_layout)
    second = paths.preflight_layout(groups(*reversed(keys)), second_layout)

    assert [
        bucket.path.relative_to(first_layout.collection_root)
        for bucket in first.buckets
    ] == [Path("archive/A.warc.gz"), Path("archive/b.warc.gz")]
    assert [
        (
            bucket.path.relative_to(second_layout.collection_root),
            bucket.urlkeys,
        )
        for bucket in second.buckets
    ] == [
        (Path("archive/A.warc.gz"), ("com,example)/A", "com,example)/a")),
        (Path("archive/b.warc.gz"), ("com,example)/b",)),
    ]


def test_preflight_allows_existing_regular_warc_and_replay_targets(tmp_path):
    collection = layout(tmp_path)
    target = paths.preferred_warc_path("com,example)/", collection)
    target.parent.mkdir(parents=True)
    target.touch()

    collection.replay_index.parent.mkdir(parents=True)
    collection.replay_index.touch()

    plan = paths.preflight_layout(groups("com,example)/"), collection)

    assert plan.buckets[0].path == target


def test_preflight_rejects_broken_final_symlink(tmp_path):
    collection = layout(tmp_path)
    target = paths.preferred_warc_path("com,example)/", collection)
    target.parent.mkdir(parents=True)
    target.symlink_to(tmp_path / "missing")

    with pytest.raises(FileExistsError, match="already exists"):
        paths.preflight_layout(groups("com,example)/"), collection)


def test_preflight_allows_symlinked_output_ancestor(tmp_path):
    real_root = tmp_path / "real-archives"
    real_root.mkdir()
    linked_root = tmp_path / "archives"
    linked_root.symlink_to(real_root, target_is_directory=True)
    collection = paths.collection_layout(
        "https://example.com/",
        root=linked_root,
    )

    plan = paths.preflight_layout(groups("com,example)/"), collection)

    assert plan.buckets[0].path.is_relative_to(linked_root)


def test_preflight_reports_non_directory_ancestor(tmp_path):
    root = tmp_path / "archives"
    root.write_text("not a directory")
    collection = paths.CollectionLayout(root, "example.com")

    with pytest.raises(OSError, match="not a directory"):
        paths.preflight_layout(groups("com,example)/"), collection)


def test_name_max_and_path_max_are_validated_independently(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "component-that-is-too-long"

    monkeypatch.setattr(
        paths,
        "_pathconf_limit",
        lambda path, name, fallback: 10 if name == "PC_NAME_MAX" else 4096,
    )
    with pytest.raises(OSError, match="NAME_MAX"):
        paths.validate_path_limits(target)

    absolute_length = len(os.fsencode(str(target.absolute()))) + 1
    monkeypatch.setattr(
        paths,
        "_pathconf_limit",
        lambda path, name, fallback: (
            255 if name == "PC_NAME_MAX" else absolute_length - 1
        ),
    )
    with pytest.raises(OSError, match="PATH_MAX"):
        paths.validate_path_limits(target)


def test_path_limit_fallbacks_are_used_when_pathconf_is_unavailable(
    tmp_path,
    monkeypatch,
):
    def unavailable(*args):
        raise OSError("unsupported")

    monkeypatch.setattr(paths.os, "pathconf", unavailable)

    paths.validate_path_limits(tmp_path / "short")


@pytest.mark.parametrize(
    "urlkey",
    ["", "com,example/path", "com,example)relative"],
)
def test_malformed_urlkeys_fail_before_export(tmp_path, urlkey):
    with pytest.raises(ValueError):
        paths.preferred_warc_path(urlkey, layout(tmp_path))


@pytest.mark.parametrize(
    ("original", "relative"),
    [
        ("https://example.com/", "website/example.com/index.html"),
        ("https://example.com/a/b/", "website/example.com/a/b/index.html"),
        ("https://example.com/about", "website/example.com/about/index.html"),
        ("https://example.com/css/style.css", "website/example.com/css/style.css"),
        (
            "https://example.com/images/logo.png",
            "website/example.com/images/logo.png",
        ),
        (
            "https://a.example.com/",
            "website/a.example.com/index.html",
        ),
    ],
)
def test_website_latest_paths(tmp_path, original, relative):
    collection = layout(tmp_path)

    result = paths.preferred_website_path(original, collection)

    assert result.relative_to(collection.collection_root) == Path(relative)


def test_website_all_paths_include_timestamp_directory(tmp_path):
    collection = layout(tmp_path)
    stamp = _timestamp("20060715085250")

    result = paths.preferred_website_path(
        "https://example.com/css/style.css",
        collection,
        timestamp=stamp,
    )

    assert result.relative_to(collection.collection_root) == Path(
        "website/example.com/20060715085250/css/style.css"
    )


def test_website_preflight_rejects_existing_file(tmp_path):
    collection = layout(tmp_path)
    target = paths.preferred_website_path("https://example.com/", collection)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        paths.preflight_website_layout(
            {
                "com,example)/": [
                    _capture(original="https://example.com/"),
                ]
            },
            collection,
            include_timestamps=False,
        )


def test_website_plan_reshapes_file_directory_conflict(tmp_path):
    collection = layout(tmp_path)
    groups = {
        "com,example)/foo.txt": [
            _capture(
                original="https://example.com/foo.txt",
                urlkey="com,example)/foo.txt",
            )
        ],
        "com,example)/foo.txt/bar": [
            _capture(
                original="https://example.com/foo.txt/bar",
                urlkey="com,example)/foo.txt/bar",
            )
        ],
    }

    plan = paths.preflight_website_layout(
        groups,
        collection,
        include_timestamps=False,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in plan.targets
    }
    assert relative == {
        "example.com/foo.txt/index.html",
        "example.com/foo.txt/bar/index.html",
    }


def test_website_paths_disambiguate_same_timestamp_by_digest(tmp_path):
    collection = layout(tmp_path)
    groups = {
        "com,example)/": [
            _capture(
                original="https://example.com/",
                captured="20170101000000",
                digest="A" * 32,
            ),
            _capture(
                original="https://example.com/",
                captured="20170101000000",
                digest="B" * 32,
            ),
        ]
    }

    plan = paths.preflight_website_layout(
        groups,
        collection,
        include_timestamps=True,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in plan.targets
    }
    assert relative == {
        "example.com/20170101000000/index--AAAAAAAA.html",
        "example.com/20170101000000/index--BBBBBBBB.html",
    }


def test_website_newest_wins_root_vs_index_html(tmp_path):
    collection = layout(tmp_path)
    older = _capture(
        original="https://example.com/index.html",
        captured="20260420004433",
        urlkey="com,example)/index.html",
        digest="A" * 32,
    )
    newer = _capture(
        original="https://example.com/",
        captured="20260511051943",
        urlkey="com,example)/",
        digest="B" * 32,
    )
    groups = {
        older.urlkey: [older],
        newer.urlkey: [newer],
    }

    plan = paths.preflight_website_layout(
        groups,
        collection,
        include_timestamps=False,
    )

    assert len(plan.targets) == 1
    assert plan.targets[0].urlkey == newer.urlkey
    assert plan.targets[0].path == (
        collection.website_root / "example.com" / "index.html"
    )
    assert "--" not in plan.targets[0].path.name


def test_website_query_folding_newest_wins(tmp_path):
    collection = layout(tmp_path)
    older = _capture(
        original="https://example.com/files/main_style.css?1546028705",
        captured="20190101000000",
        urlkey="com,example)/files/main_style.css?1546028705",
        digest="A" * 32,
    )
    newer = _capture(
        original="https://example.com/files/main_style.css?1719345030",
        captured="20240101000000",
        urlkey="com,example)/files/main_style.css?1719345030",
        digest="B" * 32,
    )
    groups = {
        older.urlkey: [older],
        newer.urlkey: [newer],
    }

    assert paths.preferred_website_path(
        older.original,
        collection,
    ) == (
        collection.website_root
        / "example.com"
        / "files"
        / "main_style.css"
    )

    plan = paths.preflight_website_layout(
        groups,
        collection,
        include_timestamps=False,
    )

    assert len(plan.targets) == 1
    assert plan.targets[0].urlkey == newer.urlkey
    assert plan.targets[0].path.name == "main_style.css"
    assert "%3F" not in plan.targets[0].path.as_posix()


@pytest.mark.parametrize(
    ("original", "relative"),
    [
        (
            "https://example.com/page?id=1",
            "website/example.com/page/index.html",
        ),
        (
            "https://example.com/search/?q=law",
            "website/example.com/search/index.html",
        ),
    ],
)
def test_website_query_folding_directory_like_paths(
    tmp_path,
    original,
    relative,
):
    collection = layout(tmp_path)

    result = paths.preferred_website_path(original, collection)

    assert result.relative_to(collection.collection_root) == Path(relative)


def test_website_paths_keep_multi_host_captures_distinct(tmp_path):
    collection = paths.collection_layout("*.example.com", root=tmp_path)
    groups = {
        "com,example,a)/": [
            _capture(
                original="https://a.example.com/",
                urlkey="com,example,a)/",
            )
        ],
        "com,example,b)/": [
            _capture(
                original="https://b.example.com/",
                urlkey="com,example,b)/",
            )
        ],
    }

    plan = paths.preflight_website_layout(
        groups,
        collection,
        include_timestamps=False,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in plan.targets
    }
    assert relative == {
        "a.example.com/index.html",
        "b.example.com/index.html",
    }
