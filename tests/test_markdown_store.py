from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import xrag.markdown_store as markdown_store
from xrag.markdown_store import MarkdownStore
from xrag.models import Post


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
        "media_urls": ("https://example.com/image.jpg",),
        "source_keywords": ("AI",),
        "source_type": "opencli",
    }
    values.update(changes)
    return Post(**values)  # type: ignore[arg-type]


def test_upsert_round_trips_utf8_body_and_media(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path / "posts", clock=lambda: "2026-08-09T00:00:00Z")

    path = store.upsert(make_post())

    assert path == tmp_path / "posts" / "123.md"
    assert "张三" in path.read_text(encoding="utf-8")
    assert path.read_text(encoding="utf-8").endswith("第一段。\n\n第二段。\n")
    assert store.read(path) == make_post(text="第一段。\n\n第二段。")


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

    assert path.read_bytes().endswith(b"original\n")
    assert [item.name for item in tmp_path.glob("*.md")] == ["abc.md"]
