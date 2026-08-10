from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(ROOT / "src"))

import xrag.dashboard_export as dashboard_export


class DashboardHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.start_tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        self.start_tags.append((tag, attributes))
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)


def _read(relative: str) -> str:
    return DASHBOARD.joinpath(*relative.split("/")).read_text(encoding="utf-8")


def _parse_html() -> DashboardHTMLParser:
    parser = DashboardHTMLParser()
    parser.feed(_read("index.html"))
    return parser


def test_dashboard_html_has_semantic_accessible_contract() -> None:
    html = _read("index.html")
    parser = _parse_html()
    required_ids = {
        "refresh-button",
        "updated-at",
        "status-banner",
        "lead-story",
        "summary-grid",
        "topic-grid",
        "hotspot-feed",
        "sort-select",
        "post-dialog",
        "dialog-content",
        "dialog-close",
        "post-template",
    }

    assert required_ids <= parser.ids
    assert '<html lang="zh-CN">' in html
    assert '<meta charset="UTF-8">' in html
    assert 'name="viewport"' in html
    assert 'name="description"' in html
    assert "<title>X-RAG 今日热点" in html
    assert "X-RAG 今日热点" in html
    assert "立即刷新" in html
    assert re.search(r'<div[^>]+id="status-banner"[^>]+aria-live="polite"', html)
    assert html.count("<section") >= 3
    assert "<h1" in html and html.count("<h2") >= 3
    assert '<script type="module" src="assets/app.js"></script>' in html
    assert '<link rel="stylesheet" href="assets/styles.css">' in html


def test_dashboard_has_no_inline_data_or_third_party_resources() -> None:
    html = _read("index.html")
    parser = _parse_html()

    assert "latest.json" not in html
    for tag, attrs in parser.start_tags:
        for attribute in ("src", "href"):
            value = attrs.get(attribute)
            if not value or value.startswith(("#", "assets/")):
                continue
            assert not value.startswith(("http://", "https://", "//")), (tag, value)


def test_dashboard_css_uses_approved_responsive_visual_system() -> None:
    css = _read("assets/styles.css")

    for token in (
        "--page: #f8fafc",
        "--card: #ffffff",
        "--ink:",
        "--pastel-green:",
        "--pastel-blue:",
        "--pastel-purple:",
        "--pastel-orange:",
        "max-width: 1180px",
        "@media (max-width: 900px)",
        "@media (max-width: 760px)",
        "@media (prefers-reduced-motion: reduce)",
        ":focus-visible",
    ):
        assert token in css
    assert "overflow-x: hidden" in css
    assert "#000000" not in css


def test_dashboard_javascript_uses_safe_rendering_and_refresh_contract() -> None:
    javascript = _read("assets/app.js")

    assert "textContent" in javascript
    assert 'image.loading = "lazy"' in javascript
    assert 'image.loading = eager ? "eager" : "lazy"' not in javascript
    assert "snapshotUrl(Date.now())" in javascript
    assert 'cache: "no-store"' in javascript
    assert 'target = "_blank"' in javascript
    assert 'rel = "noopener noreferrer"' in javascript
    assert ".showModal()" in javascript
    assert "finally" in javascript
    assert "setBusy(false)" in javascript
    for forbidden in (
        "innerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in javascript


def test_dashboard_builder_accepts_the_real_static_assets() -> None:
    prepared = dashboard_export._prepare_static_sources(ROOT, DASHBOARD)

    assert [path.as_posix() for path, _ in prepared] == [
        "index.html",
        "assets/styles.css",
        "assets/app.js",
    ]
    assert all(content for _, content in prepared)
