from pathlib import Path

from archive_magic_fetch import rewrite_local


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_rewrite_root_relative_href_to_relative(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/dir/page.html": (
                '<a href="/about.html">About</a>'
            ),
            "example.com/about.html": "<html>about</html>",
        },
    )

    summary = rewrite_local.rewrite_local_website(website)

    assert summary.rewritten == 1
    assert (
        website / "example.com" / "dir" / "page.html"
    ).read_text(encoding="utf-8") == (
        '<a href="../about.html">About</a>'
    )


def test_rewrite_does_not_guess_mime_derived_target(tmp_path):
    website = tmp_path / "website"
    original = '<a href="/download/report/">Report</a>'
    _write_tree(
        website,
        {
            "example.com/index.html": original,
            "example.com/download/report.pdf": "pdf",
        },
    )

    summary = rewrite_local.rewrite_local_website(website)

    assert summary.rewritten == 0
    assert (
        website / "example.com" / "index.html"
    ).read_text(encoding="utf-8") == original


def test_rewrite_leaves_offsite_and_missing_unchanged(tmp_path):
    website = tmp_path / "website"
    original = (
        '<script src="https://cdn.example.com/x.js"></script>'
        '<a href="/nope.html">Missing</a>'
        '<a href="mailto:hi@example.com">Mail</a>'
    )
    _write_tree(
        website,
        {
            "example.com/index.html": original,
            "example.com/about.html": "<html>about</html>",
        },
    )

    summary = rewrite_local.rewrite_local_website(website)

    assert summary.rewritten == 0
    assert (
        website / "example.com" / "index.html"
    ).read_text(encoding="utf-8") == original


def test_rewrite_absolute_same_host_and_css_url(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/index.html": (
                '<link href="https://www.example.com/files/main_style.css">'
            ),
            "example.com/files/main_style.css": (
                "body{background:url(/files/bg.png)}"
            ),
            "example.com/files/bg.png": "not-really-png",
        },
    )

    summary = rewrite_local.rewrite_local_website(website)

    assert summary.rewritten == 2
    assert (
        website / "example.com" / "index.html"
    ).read_text(encoding="utf-8") == (
        '<link href="files/main_style.css">'
    )
    assert (
        website / "example.com" / "files" / "main_style.css"
    ).read_text(encoding="utf-8") == (
        "body{background:url(bg.png)}"
    )


def test_rewrite_is_idempotent_for_relative_links(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/dir/page.html": '<a href="../about.html">About</a>',
            "example.com/about.html": "<html>about</html>",
        },
    )

    first = rewrite_local.rewrite_local_website(website)
    second = rewrite_local.rewrite_local_website(website)

    assert first.rewritten == 0
    assert second.rewritten == 0
    assert (
        website / "example.com" / "dir" / "page.html"
    ).read_text(encoding="utf-8") == (
        '<a href="../about.html">About</a>'
    )


def test_rewrite_reference_helper_resolves_homepage(tmp_path):
    website = tmp_path / "website"
    page = website / "example.com" / "contact.html"
    page.parent.mkdir(parents=True)
    page.write_text("page", encoding="utf-8")
    (website / "example.com" / "index.html").write_text("home", encoding="utf-8")

    rewritten = rewrite_local.rewrite_reference(
        "/",
        current_file=page,
        website_root=website,
        known_hosts={"example.com"},
    )

    assert rewritten == "index.html"


def test_write_and_rewrite_produces_openable_relative_links(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/index.html": (
                '<a href="/contact.html">Contact</a>'
                '<link href="/files/main_style.css?v=1">'
            ),
            "example.com/contact.html": "<html>contact</html>",
            "example.com/files/main_style.css": "body{}",
        },
    )

    rewrite_local.rewrite_local_website(website)
    home = (website / "example.com" / "index.html").read_text(encoding="utf-8")

    assert 'href="contact.html"' in home
    assert 'href="files/main_style.css"' in home
    assert (website / "example.com" / "contact.html").is_file()
    assert (website / "example.com" / "files" / "main_style.css").is_file()


def test_rewrite_does_not_alter_js_url_helper_calls(tmp_path):
    website = tmp_path / "website"
    original = (
        'function url(path) { return path; }\n'
        'const href = url("/files/main_style.css");\n'
    )
    _write_tree(
        website,
        {
            "example.com/app.js": original,
            "example.com/files/main_style.css": "body{}",
            "example.com/index.html": (
                '<style>body{background:url(/files/main_style.css)}</style>'
            ),
        },
    )

    summary = rewrite_local.rewrite_local_website(website)

    assert (website / "example.com" / "app.js").read_text(
        encoding="utf-8"
    ) == original
    assert (
        website / "example.com" / "index.html"
    ).read_text(encoding="utf-8") == (
        '<style>body{background:url(files/main_style.css)}</style>'
    )
    assert summary.rewritten == 1


def test_rewrite_latest_ignores_fourteen_digit_path_segments(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/20200101120000/page.html": (
                '<a href="/about.html">About</a>'
            ),
            "example.com/about.html": "<html>about</html>",
        },
    )

    summary = rewrite_local.rewrite_local_website(
        website,
        include_timestamps=False,
    )

    assert summary.rewritten == 1
    assert (
        website / "example.com" / "20200101120000" / "page.html"
    ).read_text(encoding="utf-8") == (
        '<a href="../about.html">About</a>'
    )


def test_rewrite_all_scopes_root_relative_to_timestamp_directory(tmp_path):
    website = tmp_path / "website"
    _write_tree(
        website,
        {
            "example.com/20200101120000/dir/page.html": (
                '<a href="/about.html">About</a>'
            ),
            "example.com/20200101120000/about.html": "<html>snap</html>",
            "example.com/about.html": "<html>host-root</html>",
        },
    )

    summary = rewrite_local.rewrite_local_website(
        website,
        include_timestamps=True,
    )

    assert summary.rewritten == 1
    assert (
        website / "example.com" / "20200101120000" / "dir" / "page.html"
    ).read_text(encoding="utf-8") == (
        '<a href="../about.html">About</a>'
    )
