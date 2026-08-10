from __future__ import annotations

import sys
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import AppConfig
from xrag.dashboard_export import DashboardBuilder, assert_public_content
from xrag.dashboard_scoring import TOPICS
from xrag.markdown_store import MarkdownStore
from xrag.models import LocalMedia, Post


QUERIES = (
    '"Autonomous AI Agents" OR "Agent Security"',
    '"World Models" OR Embodied AI',
    'RWA OR "Stablecoin Payments"',
    '"Prediction Markets" OR MiCA',
)
NOW = datetime(2026, 8, 10, 12, 34, 56, tzinfo=ZoneInfo("UTC"))


def config(root: Path) -> AppConfig:
    return AppConfig(root, False, "03:00", "Asia/Singapore", 7, 0, QUERIES, "model")


def prepare_static(root: Path) -> None:
    source = root / "dashboard"
    source.joinpath("assets").mkdir(parents=True)
    source.joinpath("index.html").write_text("<!doctype html>仪表盘", encoding="utf-8")
    source.joinpath("assets", "styles.css").write_text("body{}", encoding="utf-8")
    source.joinpath("assets", "app.js").write_text("void 0;", encoding="utf-8")


def save_post(root: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "id": "post-1",
        "author": "艾达",
        "text": "智能体安全动态",
        "created_at": NOW.isoformat(),
        "url": "https://x.com/ada/status/post-1",
        "likes": 3,
        "views": 17,
        "source_keywords": (QUERIES[0],),
    }
    values.update(overrides)
    return MarkdownStore(config(root).markdown_dir).upsert(
        Post(**values)  # type: ignore[arg-type]
    )


def seed_latest(root: Path) -> tuple[Path, bytes]:
    latest = config(root).dashboard_dir / "data" / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    previous = b'{"previous": true}\n'
    latest.write_bytes(previous)
    return latest, previous


def build(root: Path) -> dict[str, object]:
    settings = config(root)
    return DashboardBuilder(
        settings, MarkdownStore(settings.markdown_dir), clock=lambda: NOW
    ).build()


@pytest.mark.parametrize(
    "content",
    [
        '"auth_token": "secret"',
        '"AUTH-TOKEN": "secret"',
        '"auth.token": "secret"',
        '"auth token": "secret"',
        '"ct0": "secret"',
        '"Authorization": "Bearer secret"',
        r'"path": "C:\\Users\\name\\archive"',
        r'"path": "z:/private/archive"',
        '"path": "/mnt/c/private/archive"',
        '"path": "/home/name/archive"',
    ],
)
def test_assert_public_content_rejects_credentials_and_absolute_paths(content: str) -> None:
    with pytest.raises(ValueError, match="unsafe public output"):
        assert_public_content(content)


def test_assert_public_content_accepts_safe_relative_public_content() -> None:
    assert_public_content('{"url": "assets/media/image.jpg", "text": "安全"}')


def test_build_writes_exact_public_schema_topics_static_files_and_utf8(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    settings = config(tmp_path)

    result = DashboardBuilder(
        settings,
        MarkdownStore(settings.markdown_dir),
        clock=lambda: NOW,
    ).build()

    latest = settings.dashboard_dir / "data" / "latest.json"
    dated = settings.dashboard_dir / "data" / "2026-08-10T203456+0800.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert set(payload) == {
        "version",
        "generated_at",
        "timezone",
        "fallback_used",
        "summary",
        "topics",
        "posts",
    }
    assert payload["version"] == 1
    assert payload["generated_at"] == "2026-08-10T20:34:56+08:00"
    assert payload["timezone"] == "Asia/Singapore"
    assert payload["fallback_used"] is False
    assert payload["summary"] == {
        "posts": 1,
        "authors": 1,
        "media": 0,
        "engagement": 20,
    }
    assert payload["topics"] == [
        {
            "id": topic.id,
            "label": topic.label,
            "family": topic.family,
            "posts": 1 if topic == TOPICS[0] else 0,
            "score": round(payload["posts"][0]["score"], 6)
            if topic == TOPICS[0]
            else 0,
            "top_keyword": "Autonomous AI Agents" if topic == TOPICS[0] else "",
        }
        for topic in TOPICS
    ]
    assert len(payload["posts"]) == 1
    public_post = payload["posts"][0]
    assert set(public_post) == {
        "id",
        "author",
        "text",
        "created_at",
        "url",
        "likes",
        "views",
        "topic",
        "family",
        "keywords",
        "score",
        "fallback",
        "media",
    }
    assert public_post == {
        "id": "post-1",
        "author": "艾达",
        "text": "智能体安全动态",
        "created_at": NOW.isoformat(),
        "url": "https://x.com/ada/status/post-1",
        "likes": 3,
        "views": 17,
        "topic": TOPICS[0].id,
        "family": "AI",
        "keywords": [QUERIES[0]],
        "score": public_post["score"],
        "fallback": False,
        "media": [],
    }
    assert isinstance(public_post["score"], float)
    assert dated.read_bytes() == latest.read_bytes()
    assert "艾达" in latest.read_text(encoding="utf-8")
    assert latest.read_bytes().endswith(b"\n")
    assert settings.dashboard_dir.joinpath("index.html").read_text(
        encoding="utf-8"
    ) == "<!doctype html>仪表盘"
    assert settings.dashboard_dir.joinpath("assets", "styles.css").read_text() == "body{}"
    assert settings.dashboard_dir.joinpath("assets", "app.js").read_text() == "void 0;"
    assert settings.dashboard_dir.joinpath(".nojekyll").is_file()
    assert result == {
        "output_path": latest,
        "dated_snapshot_path": dated,
        "post_count": 1,
        "media_count": 0,
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/ada/status/1",
        "javascript:alert(1)",
        "https://example.com/status/1",
        "https://x.com.evil.test/status/1",
        "https://user@x.com/status/1",
        "https://user:pass@twitter.com/status/1",
    ],
)
def test_invalid_or_credentialed_non_x_url_preserves_latest(
    tmp_path: Path, url: str
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, url=url)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="X URL"):
        build(tmp_path)

    assert latest.read_bytes() == previous


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "AUTH_TOKEN=secret",
        "ct0=secret",
        "Authorization: Bearer secret",
        r"C:\private\archive",
        "/mnt/d/private/archive",
        "/home/person/archive",
    ],
)
def test_unsafe_snapshot_content_preserves_latest(
    tmp_path: Path, unsafe_text: str
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, text=unsafe_text)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="unsafe public output"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_empty_candidates_preserve_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="No valid dashboard candidates"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_summary_does_not_count_blank_author(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, author="   ")

    output_path = build(tmp_path)["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["summary"]["authors"] == 0


def test_missing_static_source_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    (tmp_path / "dashboard" / "assets" / "app.js").unlink()
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="static source is incomplete"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_exports_signature_valid_media_by_hash_without_mutating_sources(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    config(tmp_path).dashboard_source_dir.joinpath("assets", "not-copied.js").write_text(
        "private", encoding="utf-8"
    )
    media_specs = (
        ("image-01.JPG", b"\xff\xd8\xffjpeg payload", "image"),
        ("image-02.png", b"\x89PNG\r\n\x1a\npng payload", "image"),
        ("image-03.gif", b"GIF89agif payload", "image"),
        ("poster-01.WEBP", b"RIFF1234WEBPwebp payload", "video_poster"),
    )
    source_files: list[Path] = []
    local_media: list[LocalMedia] = []
    for name, content, kind in media_specs:
        source = config(tmp_path).media_dir / "post-1" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        source_files.append(source)
        local_media.append(
            LocalMedia(
                "post",
                kind,  # type: ignore[arg-type]
                f"https://pbs.twimg.com/media/{name}",
                f"../media/post-1/{name}",
                "unused/private-metadata",
            )
        )
    markdown_path = save_post(
        tmp_path,
        text="  一段带空格的说明  ",
        local_media=tuple(local_media),
    )
    source_before = {path: path.read_bytes() for path in source_files}
    markdown_before = markdown_path.read_bytes()

    result = build(tmp_path)

    latest = config(tmp_path).dashboard_dir / "data" / "latest.json"
    public_media = json.loads(latest.read_text(encoding="utf-8"))["posts"][0]["media"]
    expected_media = []
    for (name, content, kind), source in zip(media_specs, source_files, strict=True):
        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(name).suffix.lower()
        relative = f"assets/media/{digest}{suffix}"
        copied = config(tmp_path).dashboard_dir.joinpath(*relative.split("/"))
        assert copied.read_bytes() == content
        expected_media.append(
            {"url": relative, "type": kind, "alt": "一段带空格的说明"}
        )
        assert source.read_bytes() == source_before[source]
    assert public_media == expected_media
    assert markdown_path.read_bytes() == markdown_before
    assert result["media_count"] == 4
    assert json.loads(latest.read_text(encoding="utf-8"))["summary"]["media"] == 4

    copied_mtimes = {
        path: path.stat().st_mtime_ns
        for path in config(tmp_path).dashboard_dir.joinpath("assets", "media").iterdir()
    }
    build(tmp_path)
    assert {path: path.stat().st_mtime_ns for path in copied_mtimes} == copied_mtimes

    output_files = {
        path.relative_to(config(tmp_path).dashboard_dir).as_posix()
        for path in config(tmp_path).dashboard_dir.rglob("*")
        if path.is_file()
    }
    assert output_files == {
        ".nojekyll",
        "index.html",
        "assets/styles.css",
        "assets/app.js",
        "data/latest.json",
        "data/2026-08-10T203456+0800.json",
        *{item["url"] for item in expected_media},
    }


def test_missing_unsupported_and_invalid_signature_media_are_skipped(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    media_root = config(tmp_path).media_dir / "post-1"
    media_root.mkdir(parents=True)
    media_root.joinpath("unsupported.txt").write_bytes(b"GIF89a payload")
    media_root.joinpath("invalid.jpg").write_bytes(b"not a jpeg")
    items = tuple(
        LocalMedia(
            "post",
            "image",
            "https://pbs.twimg.com/media/source",
            relative,
            "private/content-type",
        )
        for relative in (
            "../media/post-1/missing.png",
            "../media/post-1/unsupported.txt",
            "../media/post-1/invalid.jpg",
        )
    )
    save_post(tmp_path, text="", local_media=items)

    result = build(tmp_path)

    output_path = result["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["posts"][0]["media"] == []
    assert payload["summary"]["media"] == 0
    assert result["media_count"] == 0
    assert not config(tmp_path).dashboard_dir.joinpath("assets", "media").exists()


@pytest.mark.parametrize("relative_path", ["../../outside.jpg", "../media"])
def test_unsafe_media_path_preserves_latest(
    tmp_path: Path, relative_path: str
) -> None:
    prepare_static(tmp_path)
    save_post(
        tmp_path,
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                relative_path,
                "image/jpeg",
            ),
        ),
    )
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media path"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_media_symlink_escape_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffoutside")
    link = config(tmp_path).media_dir / "post-1" / "linked.jpg"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    save_post(
        tmp_path,
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                "../media/post-1/linked.jpg",
                "image/jpeg",
            ),
        ),
    )
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media path"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_fallback_snapshot_and_blank_text_media_alt(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    image = config(tmp_path).media_dir / "post-1" / "image.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xffimage")
    save_post(
        tmp_path,
        author=" Ada ",
        text="   ",
        created_at="2026-08-09T12:00:00+08:00",
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                "../media/post-1/image.jpg",
                "image/jpeg",
            ),
        ),
    )

    result = build(tmp_path)

    output_path = result["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["fallback_used"] is True
    assert payload["posts"][0]["fallback"] is True
    assert payload["posts"][0]["media"][0]["alt"] == "@Ada 的配图"


def test_versioned_snapshot_write_failure_preserves_latest_and_cleans_temp(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)
    dated = latest.parent / "2026-08-10T203456+0800.json"
    dated.mkdir()

    with pytest.raises(OSError):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert list(latest.parent.glob("*.tmp")) == []
