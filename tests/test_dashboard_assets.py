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


def _css_colors(css: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", css))


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (light + 0.05) / (dark + 0.05)


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


def test_dashboard_small_text_and_focus_tokens_meet_contrast_contract() -> None:
    colors = _css_colors(_read("assets/styles.css"))

    assert {"small-text", "focus-ring"} <= colors.keys()
    for background in (
        "card",
        "pastel-green",
        "pastel-blue",
        "pastel-purple",
        "pastel-orange",
    ):
        assert _contrast(colors["small-text"], colors[background]) >= 4.5
        assert _contrast(colors["focus-ring"], colors[background]) >= 3


def test_dashboard_javascript_uses_safe_rendering_and_refresh_contract() -> None:
    javascript = _read("assets/app.js")

    assert "textContent" in javascript
    assert "const LOCAL_MEDIA_PATTERN" in javascript
    assert "LOCAL_MEDIA_PATTERN.test(value)" in javascript
    assert 'new Set(["image", "video_poster"])' in javascript
    assert 'image.loading = priority ? "eager" : "lazy"' in javascript
    assert 'image.fetchPriority = "high"' in javascript
    assert javascript.count("priority: true") == 1
    assert 'label: "热点卡片媒体占位图"' in javascript
    assert 'label: "热点详情媒体占位图"' in javascript
    assert 'image.addEventListener("error", () => image.remove()' in javascript
    assert 'container.setAttribute("role", "img")' in javascript
    assert 'container.removeAttribute("role")' in javascript
    assert 'container.removeAttribute("aria-label")' in javascript
    assert "snapshotUrl(clock())" in javascript
    assert "clock: Date.now" in javascript
    assert 'cache: "no-store"' in javascript
    assert 'target = "_blank"' in javascript
    assert 'rel = "noopener noreferrer"' in javascript
    assert ".showModal()" in javascript
    assert "finally" in javascript
    assert "setBusy(false)" in javascript
    assert "loadSnapshotState" in javascript
    assert "failed-with-existing" in javascript
    assert "刷新失败，继续展示上次数据" in javascript
    assert "当前已是最新数据" in javascript
    assert "已载入新数据" in javascript
    assert 'heading.textContent = `@${post.author || "未知作者"} · 热点详情`' in javascript
    assert 'title.textContent = `@${post.author || "未知作者"} · 今日领衔`' in javascript
    for forbidden in (
        "innerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in javascript


def test_dashboard_has_mobile_overflow_and_dialog_containment_contract() -> None:
    html = _read("index.html")
    css = _read("assets/styles.css")

    assert 'class="post-media media-placeholder" role="img"' in html
    assert "overflow-wrap: anywhere" in css
    assert "overscroll-behavior: contain" in css
    assert "min-width: 0" in css


def test_hidden_elements_override_component_display_rules() -> None:
    css = _read("assets/styles.css")

    assert re.search(
        r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important",
        css,
        flags=re.DOTALL,
    )


def test_dashboard_builder_accepts_the_real_static_assets() -> None:
    prepared = dashboard_export._prepare_static_sources(ROOT, DASHBOARD)

    assert [path.as_posix() for path, _ in prepared] == [
        "index.html",
        "assets/styles.css",
        "assets/app.js",
    ]
    assert all(content for _, content in prepared)
