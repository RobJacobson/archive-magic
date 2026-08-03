import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from wayback import CdxRecord

from archive_magic_fetch import collection_paths


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
    return collection_paths.collection_paths(pattern, root=tmp_path / "archives")


def groups(*urlkeys):
    return {
        ("example.com", urlkey): [_capture(urlkey=urlkey)]
        for urlkey in urlkeys
    }


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("https://Kevin.Burke.Dev/", "kevin.burke.dev"),
        ("http://www.example.com/*", "example.com"),
        ("http://www1.example.com/*", "example.com"),
        ("http://www12.example.com/*", "example.com"),
        ("*.example.com", "example.com"),
        ("https://example.com:443/*", "example.com"),
        ("http://example.com:80/*", "example.com"),
        ("https://example.com:8443/*", "example.com%3A8443"),
        ("example.com:443/*", "example.com%3A443"),
        ("https://münich.example/*", "xn--mnich-kva.example"),
        ("https://example.com./", "example.com"),
    ],
)
def test_collection_name_normalization(pattern, expected):
    assert collection_paths.normalize_collection_name(pattern) == expected


def test_collection_idna_uses_the_python_codec(monkeypatch):
    monkeypatch.setitem(
        __import__("sys").modules,
        "idna",
        object(),
    )

    assert (
        collection_paths.normalize_collection_name("https://münich.example/")
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
        collection_paths.normalize_collection_name(pattern)


@pytest.mark.parametrize(
    ("urlkey", "relative"),
    [
        ("com,example)/", "archive/example.com/index.warc.gz"),
        ("com,example)/about", "archive/example.com/about.warc.gz"),
        ("com,example)/posts", "archive/example.com/posts.warc.gz"),
        ("com,example)/posts/", "archive/example.com/posts/index.warc.gz"),
        (
            "com,example)/posts/hello-world",
            "archive/example.com/posts/hello-world.warc.gz",
        ),
        (
            "com,example)/images/logo.png",
            "archive/example.com/images/logo.png.warc.gz",
        ),
        (
            "com,example)/image.png?size=2",
            "archive/example.com/image.png%3Fsize%3D2.warc.gz",
        ),
        (
            "com,example)/?view=full",
            "archive/example.com/index%3Fview%3Dfull.warc.gz",
        ),
        (
            "com,example)/posts/?view=full",
            "archive/example.com/posts/index%3Fview%3Dfull.warc.gz",
        ),
    ],
)
def test_readable_urlkey_paths(tmp_path, urlkey, relative):
    collection = layout(tmp_path)

    result = collection_paths.preferred_warc_path(
        urlkey,
        "https://example.com/",
        collection,
    )

    assert result.relative_to(collection.collection_root) == Path(relative)
    assert "--" not in result.name


@pytest.mark.parametrize(
    ("original", "folder"),
    [
        ("https://example.com/", "example.com"),
        ("https://www.Example.com.:443/", "example.com"),
        ("https://www1.Example.com.:443/", "example.com"),
        ("http://example.com:80/", "example.com"),
        ("https://example.com:8443/", "example.com%3A8443"),
        ("http://example.com:8080/", "example.com%3A8080"),
        ("https://münich.example/", "xn--mnich-kva.example"),
        ("https://[2001:db8::1]:8443/", "%5B2001%3Adb8%3A%3A1%5D%3A8443"),
    ],
)
def test_warc_domain_folders_normalize_authorities(tmp_path, original, folder):
    collection = layout(tmp_path)

    result = collection_paths.preferred_warc_path(
        "com,example)/",
        original,
        collection,
    )

    assert result == collection.archive_root / folder / "index.warc.gz"


def test_identical_resource_paths_on_different_domains_never_share_warc(
    tmp_path,
):
    collection = layout(tmp_path)
    first = _capture(original="https://first.example/index.html")
    second = _capture(
        original="https://second.example/index.html",
        urlkey="example,second)/index.html",
    )

    allocated = collection_paths.allocate_warc_paths(
        {
            ("first.example", first.urlkey): [first],
            ("second.example", second.urlkey): [second],
        },
        collection,
    )

    assert set(allocated) == {
        collection.archive_root / "first.example" / "index.warc.gz",
        collection.archive_root / "second.example" / "index.html.warc.gz",
    }


def test_nondefault_port_folder_cannot_collide_with_default_port(tmp_path):
    collection = layout(tmp_path)
    default = _capture(original="https://example.com/")
    nondefault = _capture(original="https://example.com:8443/")

    allocated = collection_paths.allocate_warc_paths(
        {
            ("example.com", default.urlkey): [default],
            ("example.com%3A8443", nondefault.urlkey): [nondefault],
        },
        collection,
    )

    assert set(allocated) == {
        collection.archive_root / "example.com" / "index.warc.gz",
        collection.archive_root
        / "example.com%3A8443"
        / "index.warc.gz",
    }


def test_unsafe_segments_cannot_reshape_the_collection(tmp_path):
    collection = layout(tmp_path)

    result = collection_paths.preferred_warc_path(
        "com,example)/a//../CON./file:name",
        "https://example.com/",
        collection,
    )

    assert result.relative_to(collection.archive_root).parts == (
        "example.com",
        "a",
        "%00",
        "%2E%2E",
        "CON%2E",
        "file%3Aname.warc.gz",
    )
    assert result.is_relative_to(collection.archive_root)


def test_overlong_component_is_bounded_without_hash(tmp_path):
    collection = layout(tmp_path)
    result = collection_paths.preferred_warc_path(
        f"com,example)/{'x' * 400}",
        "https://example.com/",
        collection,
    )

    assert len(result.name.encode("ascii")) == collection_paths.MAX_COMPONENT_BYTES
    assert result.name.endswith(".warc.gz")
    assert "--" not in result.name


def test_truncation_reencodes_an_exposed_trailing_dot(tmp_path):
    collection = layout(tmp_path)
    component = f"{'a' * 239}.tail"

    result = collection_paths.preferred_warc_path(
        f"com,example)/{component}/resource",
        "https://example.com/",
        collection,
    )
    directory = result.relative_to(collection.archive_root).parts[1]

    assert len(directory.encode("ascii")) <= collection_paths.MAX_COMPONENT_BYTES
    assert directory.endswith("%2E")
    assert not directory.endswith(".")


def test_truncation_reencodes_a_trailing_dot_run_within_limit(tmp_path):
    collection = layout(tmp_path)
    component = f"{'.' * collection_paths.MAX_COMPONENT_BYTES}tail"

    result = collection_paths.preferred_warc_path(
        f"com,example)/{component}/resource",
        "https://example.com/",
        collection,
    )
    directory = result.relative_to(collection.archive_root).parts[1]

    assert len(directory.encode("ascii")) <= collection_paths.MAX_COMPONENT_BYTES
    assert directory.endswith("%2E")
    assert not directory.endswith(".")


def test_overlong_collection_name_is_rejected_instead_of_merged():
    label = "a" * 63
    host = ".".join([label, label, label, "a" * 50])

    with pytest.raises(ValueError, match="collection name exceeds"):
        collection_paths.normalize_collection_name(f"https://{host}/")


def test_intentional_and_case_equivalent_paths_share_warcs(tmp_path):
    collection = layout(tmp_path)
    allocated = collection_paths.allocate_warc_paths(
        groups(
            "com,example)/posts/",
            "com,example)/posts/index",
            "com,example)/Posts/index",
        ),
        collection,
    )

    assert allocated == {
        collection.archive_root / "example.com" / "Posts" / "index.warc.gz": (
            ("example.com", "com,example)/Posts/index"),
            ("example.com", "com,example)/posts/"),
            ("example.com", "com,example)/posts/index"),
        )
    }


def test_urlkey_authority_does_not_shape_warc_path(tmp_path):
    collection = layout(tmp_path)
    keys = (
        "com,domain)/posts/",
        "com,domain,www)/posts/",
        "com,domain,blog)/posts/",
    )

    allocated = collection_paths.allocate_warc_paths(groups(*keys), collection)

    assert allocated == {
        collection.archive_root / "example.com" / "posts" / "index.warc.gz": tuple(
            ("example.com", key) for key in sorted(keys)
        )
    }


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
def test_file_directory_conflicts_share_ancestor_warc(
    tmp_path,
    parent,
    descendant,
):
    collection = layout(tmp_path)

    allocated = collection_paths.allocate_warc_paths(
        groups(descendant, parent),
        collection,
    )

    assert allocated == {
        collection_paths.preferred_warc_path(
            parent,
            "https://example.com/",
            collection,
        ): tuple(
            ("example.com", key) for key in sorted((parent, descendant))
        )
    }


def test_warc_spelling_and_order_ignore_search_order(tmp_path):
    first_layout = layout(tmp_path / "first")
    second_layout = layout(tmp_path / "second")
    keys = ("com,example)/b", "com,example)/A", "com,example)/a")

    first = collection_paths.allocate_warc_paths(groups(*keys), first_layout)
    second = collection_paths.allocate_warc_paths(
        groups(*reversed(keys)),
        second_layout,
    )

    assert [
        path.relative_to(first_layout.collection_root)
        for path in first
    ] == [Path("archive/example.com/A.warc.gz"), Path("archive/example.com/b.warc.gz")]
    assert [
        (
            path.relative_to(second_layout.collection_root),
            urlkeys,
        )
        for path, urlkeys in second.items()
    ] == [
        (
            Path("archive/example.com/A.warc.gz"),
            (
                ("example.com", "com,example)/A"),
                ("example.com", "com,example)/a"),
            ),
        ),
        (
            Path("archive/example.com/b.warc.gz"),
            (("example.com", "com,example)/b"),),
        ),
    ]


def test_warc_allocation_does_not_inspect_output_filesystem(tmp_path):
    collection = layout(tmp_path)
    target = collection_paths.preferred_warc_path(
        "com,example)/",
        "https://example.com/",
        collection,
    )
    target.parent.mkdir(parents=True)
    target.touch()
    collection.replay_index.parent.mkdir(parents=True)
    collection.replay_index.touch()
    target.with_name(target.name + ".tmp").touch()

    assert collection_paths.allocate_warc_paths(
        groups("com,example)/"),
        collection,
    ) == {target: (("example.com", "com,example)/"),)}


def test_warc_allocation_constructs_one_path_per_group(tmp_path, monkeypatch):
    collection = layout(tmp_path)
    selected = groups(*(f"com,example)/resource-{index}" for index in range(5000)))
    original = collection_paths.preferred_warc_path
    calls = 0

    def counted(urlkey, original_url, selected_layout):
        nonlocal calls
        calls += 1
        return original(urlkey, original_url, selected_layout)

    monkeypatch.setattr(collection_paths, "preferred_warc_path", counted)

    allocated = collection_paths.allocate_warc_paths(selected, collection)

    assert len(allocated) == len(selected)
    assert calls == len(selected)


def test_name_max_and_path_max_are_validated_independently(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "component-that-is-too-long"

    monkeypatch.setattr(
        collection_paths,
        "_pathconf_limit",
        lambda path, name, fallback: 10 if name == "PC_NAME_MAX" else 4096,
    )
    with pytest.raises(OSError, match="NAME_MAX"):
        collection_paths.validate_path_limits(target)

    absolute_length = len(os.fsencode(str(target.absolute()))) + 1
    monkeypatch.setattr(
        collection_paths,
        "_pathconf_limit",
        lambda path, name, fallback: (
            255 if name == "PC_NAME_MAX" else absolute_length - 1
        ),
    )
    with pytest.raises(OSError, match="PATH_MAX"):
        collection_paths.validate_path_limits(target)


def test_path_limit_fallbacks_are_used_when_pathconf_is_unavailable(
    tmp_path,
    monkeypatch,
):
    def unavailable(*args):
        raise OSError("unsupported")

    monkeypatch.setattr(collection_paths.os, "pathconf", unavailable)

    collection_paths.validate_path_limits(tmp_path / "short")


@pytest.mark.parametrize(
    "urlkey",
    ["", "com,example/path", "com,example)relative"],
)
def test_malformed_urlkeys_fail_before_warc_allocation(tmp_path, urlkey):
    with pytest.raises(ValueError):
        collection_paths.preferred_warc_path(
            urlkey,
            "https://example.com/",
            layout(tmp_path),
        )


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

    result = collection_paths.preferred_website_path(
        original,
        collection,
        mimetype="text/html",
    )

    assert result.relative_to(collection.collection_root) == Path(relative)


def test_website_all_paths_include_timestamp_directory(tmp_path):
    collection = layout(tmp_path)
    stamp = _timestamp("20060715085250")

    result = collection_paths.preferred_website_path(
        "https://example.com/css/style.css",
        collection,
        mimetype="text/css",
        timestamp=stamp,
    )

    assert result.relative_to(collection.collection_root) == Path(
        "website/example.com/20060715085250/css/style.css"
    )


@pytest.mark.parametrize(
    ("original", "mimetype", "relative"),
    [
        (
            "https://example.com/about",
            "text/html",
            "website/example.com/about/index.html",
        ),
        (
            "https://example.com/download/annual-report",
            "application/pdf",
            "website/example.com/download/annual-report.pdf",
        ),
        (
            "https://example.com/download/report/",
            "application/pdf",
            "website/example.com/download/report.pdf",
        ),
        (
            "https://example.com/",
            "application/pdf",
            "website/example.com/index.pdf",
        ),
        (
            "https://example.com/download/report",
            "application/octet-stream",
            "website/example.com/download/report",
        ),
    ],
)
def test_website_paths_follow_mime_semantics(
    tmp_path,
    original,
    mimetype,
    relative,
):
    collection = layout(tmp_path)

    result = collection_paths.preferred_website_path(
        original,
        collection,
        mimetype=mimetype,
    )

    assert result.relative_to(collection.collection_root) == Path(relative)


def test_website_preflight_rejects_existing_file(tmp_path):
    collection = layout(tmp_path)
    target = collection_paths.preferred_website_path(
        "https://example.com/",
        collection,
        mimetype="text/html",
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    with pytest.raises(FileExistsError, match="already exists"):
        collection_paths.prepare_website_files(
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

    website_files = collection_paths.prepare_website_files(
        groups,
        collection,
        include_timestamps=False,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in website_files.targets
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

    website_files = collection_paths.prepare_website_files(
        groups,
        collection,
        include_timestamps=True,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in website_files.targets
    }
    assert relative == {
        "example.com/20170101000000/index--AAAAAAAA.html",
        "example.com/20170101000000/index--BBBBBBBB.html",
    }


def test_website_paths_reject_identical_capture_collision(tmp_path):
    collection = layout(tmp_path)
    duplicate = _capture(
        original="https://example.com/",
        captured="20170101000000",
        digest="A" * 32,
    )

    with pytest.raises(FileExistsError, match="identical digests"):
        collection_paths.prepare_website_files(
            {"com,example)/": [duplicate, duplicate]},
            collection,
            include_timestamps=True,
        )


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

    website_files = collection_paths.prepare_website_files(
        groups,
        collection,
        include_timestamps=False,
    )

    assert len(website_files.targets) == 1
    assert website_files.targets[0].capture.urlkey == newer.urlkey
    assert website_files.targets[0].path == (
        collection.website_root / "example.com" / "index.html"
    )
    assert "--" not in website_files.targets[0].path.name


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

    assert collection_paths.preferred_website_path(
        older.original,
        collection,
        mimetype=older.mimetype,
    ) == (
        collection.website_root
        / "example.com"
        / "files"
        / "main_style.css"
    )

    website_files = collection_paths.prepare_website_files(
        groups,
        collection,
        include_timestamps=False,
    )

    assert len(website_files.targets) == 1
    assert website_files.targets[0].capture.urlkey == newer.urlkey
    assert website_files.targets[0].path.name == "main_style.css"
    assert "%3F" not in website_files.targets[0].path.as_posix()


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

    result = collection_paths.preferred_website_path(
        original,
        collection,
        mimetype="text/html",
    )

    assert result.relative_to(collection.collection_root) == Path(relative)


def test_website_paths_keep_multi_host_captures_distinct(tmp_path):
    collection = collection_paths.collection_paths("*.example.com", root=tmp_path)
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

    website_files = collection_paths.prepare_website_files(
        groups,
        collection,
        include_timestamps=False,
    )

    relative = {
        target.path.relative_to(collection.website_root).as_posix()
        for target in website_files.targets
    }
    assert relative == {
        "a.example.com/index.html",
        "b.example.com/index.html",
    }
