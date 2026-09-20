"""No page of this interface may reach off the host.

DESIGN.md §10's image is read-only and assumes no network egress, so a CDN
reference is a panel that breaks in the deployment it was built for (design D3).
Both halves are checked against the template source rather than against a
rendered page, because a template that is only rendered on one route would
otherwise be checked on one route.

"Every `src`/`href` resolves under `static/`" is read as it is meant: every
*asset* reference — a `<script src>`, a `<link href>`, an `<img src>` — names a
file that exists in `web/static/`. A `<a href="/contacts">` is navigation, not
an asset, and is held to the other rule instead: it must be a path on this
application, never an absolute URL.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import sighop.web

WEB_DIR = Path(sighop.web.__file__).parent
TEMPLATE_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
STATIC_PREFIX = "/static/"

# `src=` on any element, and `href=` on a <link> — the two ways a template can
# make the browser fetch something. Ordinary <a href> navigation is excluded by
# requiring the tag name for `href`.
ASSET_REF = re.compile(
    r"""(?:<link\b[^>]*?\bhref|\bsrc)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE | re.DOTALL,
)
ANY_REF = re.compile(r"""\b(?:src|href|action)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
EXTERNAL = re.compile(r"""["'(\s](?:https?:)?//[^"'\s)]+""", re.IGNORECASE)

# `data:` and `mailto:` reach no network; `#` is an anchor on the page itself.
LOCAL_SCHEMES = ("data:", "mailto:", "#", "javascript:void")


def _templates() -> list[Path]:
    return sorted(TEMPLATE_DIR.rglob("*.html"))


def test_the_template_and_static_trees_exist() -> None:
    assert TEMPLATE_DIR.is_dir()
    assert STATIC_DIR.is_dir()


def test_htmx_is_vendored_with_its_version_and_licence_recorded() -> None:
    vendored = STATIC_DIR / "htmx.min.js"
    assert vendored.is_file(), "htmx.min.js is not vendored; the panel would need a CDN"
    notes = (STATIC_DIR / "VENDORED.md").read_text()
    assert "htmx.min.js" in notes
    assert re.search(r"\*\*Version\*\*:\s*\d+\.\d+", notes), "the vendored version is not recorded"
    assert "License" in notes and "0BSD" in notes, "the vendored licence is not recorded"


@pytest.mark.parametrize(
    "path", _templates() or [None], ids=lambda p: "none" if p is None else p.name
)
def test_no_template_references_an_external_origin(path: Path | None) -> None:
    if path is None:
        pytest.skip("no templates yet")
    found = EXTERNAL.findall(path.read_text())
    assert not found, (
        f"{path.name} references {found}, which the browser would fetch from another "
        "origin; every asset this interface needs is served by the application"
    )


@pytest.mark.parametrize(
    "path", _templates() or [None], ids=lambda p: "none" if p is None else p.name
)
def test_every_asset_reference_resolves_under_static(path: Path | None) -> None:
    if path is None:
        pytest.skip("no templates yet")
    unresolved: list[str] = []
    for ref in ASSET_REF.findall(path.read_text()):
        if ref.startswith(LOCAL_SCHEMES) or "{{" in ref:
            continue
        if not ref.startswith(STATIC_PREFIX):
            unresolved.append(ref)
            continue
        if not (STATIC_DIR / ref.removeprefix(STATIC_PREFIX)).is_file():
            unresolved.append(ref)
    assert not unresolved, (
        f"{path.name} names assets that are not files under static/: {unresolved}"
    )


@pytest.mark.parametrize(
    "path", _templates() or [None], ids=lambda p: "none" if p is None else p.name
)
def test_every_navigation_target_is_a_path_on_this_application(path: Path | None) -> None:
    if path is None:
        pytest.skip("no templates yet")
    offenders = [
        ref
        for ref in ANY_REF.findall(path.read_text())
        if not ref.startswith(LOCAL_SCHEMES) and not ref.startswith("/") and "{{" not in ref
    ]
    assert not offenders, (
        f"{path.name} links to {offenders}; every target must be an absolute path on "
        "this application so no page depends on where it was served from"
    )


@pytest.mark.parametrize(
    "path", _templates() or [None], ids=lambda p: "none" if p is None else p.name
)
def test_every_table_scrolls_inside_its_own_box(path: Path | None) -> None:
    """A table wider than the viewport must scroll in place rather than widen
    the page (`web-display`). The panel's tables run to ten and thirteen
    columns, so on a phone this is every one of them.

    Read as the convention it is: the line opening a table is preceded by the
    line opening its wrapper, which is what the panel's own templates do.
    """
    if path is None:
        pytest.skip("no templates yet")
    lines = path.read_text().splitlines()
    bare: list[int] = []
    for number, line in enumerate(lines, start=1):
        if not re.match(r"\s*<table\b", line):
            continue
        before = next((earlier for earlier in reversed(lines[: number - 1]) if earlier.strip()), "")
        if 'class="table-wrap"' not in before:
            bare.append(number)
    assert not bare, (
        f"{path.name} opens a table outside a .table-wrap at line(s) {bare}; it would "
        "widen the whole page on a narrow viewport"
    )
