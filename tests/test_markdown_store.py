from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import xrag.markdown_store as markdown_store
from xrag.markdown_store import MarkdownStore
from xrag.models import LocalMedia, Post, QuotedPost


def make_post(**changes: object) -> Post:
    values: dict[str, object] = {
        "id": "123",
        "author": "张三",
        "text": "  第一段。\n\n第二段。  ",
        "created_at": "2026-08-08T10:30:00Z",
        "url": "https://x.com/example/status/123",
        "bio": "AI researcher",
        "likes": 5,
        "views": 100,
        "media_urls": ("https://pbs.twimg.com/media/image",),
        "media_posters": ("https://pbs.twimg.com/media/poster",),
        "quoted_post": QuotedPost(
            "456",
            "quoted",
            "quoted body",
            "2026-08-07T00:00:00Z",
            "https://x.com/quoted/status/456",
            media_urls=("https://pbs.twimg.com/media/quoted",),
        ),
        "local_media": (
            LocalMedia("post", "image", "https://pbs.twimg.com/media/image", "../media/123/image-01.jpg", "image/jpeg"),
            LocalMedia("post", "video_poster", "https://pbs.twimg.com/media/poster", "../media/123/video-poster-01.jpg", "image/jpeg"),
            LocalMedia("quoted", "image", "https://pbs.twimg.com/media/quoted", "../media/123/quoted-image-01.jpg", "image/jpeg"),
        ),
        "source_keywords": ("AI",),
        "source_type": "opencli",
    }
    values.update(changes)
    return Post(**values)  # type: ignore[arg-type]


def test_upsert_round_trips_readable_utf8_body_and_media(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "posts", clock=lambda: "2026-08-09T00:00:00Z")

    path = store.upsert(make_post())

    assert path == tmp_path / "posts" / "123.md"
    content = path.read_text(encoding="utf-8")
    assert content.count("# @张三的推文") == 1
    assert "## 正文" in content
    assert "<!-- xrag:text:start -->" in content
    assert "<!-- xrag:text:end -->" in content
    assert "![图片 1](../media/123/image-01.jpg)" in content
    assert "![视频封面 1](../media/123/video-poster-01.jpg)" in content
    assert "## 引用推文" in content
    assert "> @quoted：quoted body" in content
    assert "![引用图片 1](../media/123/quoted-image-01.jpg)" in content
    assert "[查看 X 原文](https://x.com/example/status/123)" in content
    assert store.read(path) == make_post(text="第一段。\n\n第二段。")


def test_read_supports_legacy_body_and_optional_fields(tmp_path: Path) -> None:
    path = tmp_path / "legacy.md"
    path.write_text(
        """---
id: legacy
author: Ada
author_bio: ''
created_at: ''
collected_at: '2026-08-09T00:00:00Z'
updated_at: '2026-08-09T00:00:00Z'
url: https://x.com/i/status/legacy
likes: 0
views: 0
media_urls: []
source_keywords: []
source_type: opencli
---

legacy body
""",
        encoding="utf-8",
    )

    post = MarkdownStore(tmp_path).read(path)

    assert post.text == "legacy body"
    assert post.media_posters == ()
    assert post.quoted_post is None
    assert post.local_media == ()


def test_get_loads_existing_post_and_validates_id(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    expected = make_post(text="stored")
    store.upsert(expected)

    assert store.get("123") == expected
    assert store.get("missing") is None
    with pytest.raises(ValueError, match="unsafe post ID"):
        store.get("../escape")


def test_read_rejects_malformed_optional_media_metadata(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post())
    content = path.read_text(encoding="utf-8").replace(
        "owner: post", "owner: invalid", 1
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="local_media"):
        store.read(path)


def test_second_upsert_refreshes_post_and_merges_keywords(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path, clock=lambda: "2026-08-09T00:00:00Z")
    store.upsert(make_post(source_keywords=("AI",)))

    path = store.upsert(
        make_post(
            author="李四",
            text="new text",
            likes=11,
            views=200,
            source_keywords=("GPU", "AI", "GPU"),
        )
    )

    assert list(tmp_path.glob("*.md")) == [path]
    post = store.read(path)
    assert post.author == "李四"
    assert post.text == "new text"
    assert (post.likes, post.views) == (11, 200)
    assert post.source_keywords == ("AI", "GPU")


def test_update_preserves_collected_at_and_refreshes_updated_at(tmp_path: Path) -> None:
    timestamps = iter(("2026-08-09T00:00:00Z", "2026-08-09T01:00:00Z"))
    store = MarkdownStore(tmp_path, clock=lambda: next(timestamps))
    path = store.upsert(make_post())
    original = path.read_text(encoding="utf-8")

    store.upsert(make_post(likes=6))
    updated = path.read_text(encoding="utf-8")

    assert "collected_at: '2026-08-09T00:00:00Z'" in original
    assert "collected_at: '2026-08-09T00:00:00Z'" in updated
    assert "updated_at: '2026-08-09T01:00:00Z'" in updated


def test_read_rejects_missing_or_invalid_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "broken.md"
    path.write_text("not front matter\n", encoding="utf-8")

    with pytest.raises(ValueError, match="front matter"):
        MarkdownStore(tmp_path).read(path)


def test_rejects_unsafe_ids_and_iterates_sorted_or_empty(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "missing")
    assert list(store.iter_posts()) == []

    with pytest.raises(ValueError, match="unsafe post ID"):
        store.upsert(make_post(id="../escape"))

    store.upsert(make_post(id="z"))
    store.upsert(make_post(id="10"))

    assert [path.name for path, _ in store.iter_posts()] == ["10.md", "z.md"]


def test_failed_update_preserves_original_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post(text="original"))
    original = path.read_bytes()

    def fail_replace(source: object, target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(markdown_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.upsert(make_post(text="replacement"))

    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_first_write_leaves_no_canonical_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MarkdownStore(tmp_path)

    def fail_replace(source: object, target: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(markdown_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        store.upsert(make_post())

    assert list(tmp_path.glob("*.md")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_rejects_casefolded_id_collision_and_preserves_existing_post(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post(id="abc", text="original"))

    with pytest.raises(ValueError, match="case-insensitive collision"):
        store.upsert(make_post(id="ABC", text="replacement"))

    assert b"original" in path.read_bytes()
    assert [item.name for item in tmp_path.glob("*.md")] == ["abc.md"]
