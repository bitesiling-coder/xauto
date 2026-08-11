from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import xrag.markdown_store as markdown_store
from xrag.markdown_store import MarkdownStore, extract_body_text, extract_body_translation
from xrag.models import LocalMedia, Post, QuotedPost, TranslationMetadata


def translation_for(text: str, **changes: object) -> TranslationMetadata:
    values: dict[str, object] = {
        "language": "zh-CN",
        "model_id": "translator-v1",
        "revision": "r1",
        "source_sha256": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
        "translated_at": "2026-08-10T00:00:00Z",
    }
    values.update(changes)
    return TranslationMetadata(**values)  # type: ignore[arg-type]


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
            media_urls=(
                "https://pbs.twimg.com/media/quoted",
                "https://video.twimg.com/ext_tw_video/quoted.mp4",
            ),
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
    assert "[打开引用原视频](https://video.twimg.com/ext_tw_video/quoted.mp4)" in content
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
    assert post.text_zh == ""
    assert post.translation_zh is None


def test_read_supports_old_canonical_body_without_translation(tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post(text="original"))

    post = store.read(path)

    assert post.text == "original"
    assert post.text_zh == ""
    assert post.translation_zh is None


def test_upsert_round_trips_main_translation_and_renders_before_media(tmp_path: Path) -> None:
    text = "  exact original\nwith formatting  "
    text_zh = "第一行\n\n  缩进保留\n最后一行"
    metadata = translation_for(text)
    store = MarkdownStore(tmp_path)

    path = store.upsert(make_post(text=text, text_zh=text_zh, translation_zh=metadata))

    content = path.read_text(encoding="utf-8")
    expected = (
        "## 中文翻译（机器翻译）\n\n"
        "<!-- xrag:text-zh:start -->\n"
        f"{text_zh}\n"
        "<!-- xrag:text-zh:end -->"
    )
    assert expected in content
    assert content.index("<!-- xrag:text:end -->") < content.index(expected)
    assert content.index(expected) < content.index("## 媒体")
    assert "translation_zh:" in content
    assert store.read(path) == make_post(
        text=text.strip(), text_zh=text_zh, translation_zh=metadata
    )


def test_upsert_round_trips_quoted_translation_and_renders_readable_blockquote(
    tmp_path: Path,
) -> None:
    quoted_text = "quoted original"
    quoted_zh = "引用第一行\n\n引用第三行"
    quoted = QuotedPost(
        "456",
        "quoted",
        quoted_text,
        "2026-08-07T00:00:00Z",
        "https://x.com/quoted/status/456",
        text_zh=quoted_zh,
        translation_zh=translation_for(quoted_text),
    )
    store = MarkdownStore(tmp_path)

    path = store.upsert(make_post(quoted_post=quoted))

    content = path.read_text(encoding="utf-8")
    assert (
        "> @quoted：quoted original\n\n"
        "> **中文翻译（机器翻译）**\n"
        "> 引用第一行\n"
        ">\n"
        "> 引用第三行"
    ) in content
    assert store.read(path).quoted_post == quoted


def test_upsert_normalizes_quoted_translation_newline_boundaries(tmp_path: Path) -> None:
    quoted_text = "quoted original"
    quoted = QuotedPost(
        "456", "quoted", quoted_text, "", "https://x.com/i/status/456",
        text_zh="\n引用译文\n", translation_zh=translation_for(quoted_text),
    )

    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post(quoted_post=quoted))
    post = store.read(path)

    assert post.quoted_post is not None
    assert post.quoted_post.text_zh == "引用译文"
    assert "> **中文翻译（机器翻译）**\n> 引用译文" in path.read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "text_zh, metadata",
    [
        ("", translation_for("original")),
        ("   ", translation_for("original")),
        ("译文", None),
    ],
)
def test_upsert_rejects_main_translation_half_pairs_before_creating_directory(
    tmp_path: Path, text_zh: str, metadata: TranslationMetadata | None
) -> None:
    target = tmp_path / "missing" / "posts"

    with pytest.raises(ValueError, match="text_zh.*translation_zh"):
        MarkdownStore(target).upsert(
            make_post(text="original", text_zh=text_zh, translation_zh=metadata)
        )

    assert not target.exists()


def test_upsert_rejects_stale_main_and_quoted_translation_hashes_before_writing(
    tmp_path: Path,
) -> None:
    store = MarkdownStore(tmp_path / "missing")
    stale = translation_for("different")
    with pytest.raises(ValueError, match="source_sha256"):
        store.upsert(make_post(text="original", text_zh="译文", translation_zh=stale))
    assert not store.directory.exists()

    quoted = QuotedPost(
        "456", "quoted", "quoted original", "", "https://x.com/i/status/456",
        text_zh="引用译文", translation_zh=stale,
    )
    with pytest.raises(ValueError, match="source_sha256"):
        store.upsert(make_post(quoted_post=quoted))
    assert not store.directory.exists()


def test_upsert_rejects_quoted_translation_half_pair_before_creating_directory(
    tmp_path: Path,
) -> None:
    target = tmp_path / "missing" / "posts"
    quoted = QuotedPost(
        "456", "quoted", "quoted original", "", "https://x.com/i/status/456",
        text_zh="引用译文",
    )

    with pytest.raises(ValueError, match="quoted_tweet.translation_zh"):
        MarkdownStore(target).upsert(make_post(quoted_post=quoted))

    assert not target.exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"language": "zh"},
        {"model_id": " "},
        {"revision": ""},
        {"source_sha256": "A" * 64},
        {"source_sha256": "0" * 63},
        {"translated_at": "\t"},
    ],
)
def test_upsert_rejects_invalid_translation_metadata(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        MarkdownStore(tmp_path / "missing").upsert(
            make_post(
                text="original",
                text_zh="译文",
                translation_zh=translation_for("original", **changes),
            )
        )


@pytest.mark.parametrize(
    "replacement",
    [
        "translation_zh: []",
        "translation_zh:\n  language: zh-CN",
        (
            "translation_zh:\n  language: zh-CN\n  model_id: m\n  revision: r\n"
            "  source_sha256: '" + "0" * 64 + "'\n  translated_at: now\n  extra: no"
        ),
        (
            "translation_zh:\n  language: 7\n  model_id: m\n  revision: r\n"
            "  source_sha256: '" + "0" * 64 + "'\n  translated_at: now"
        ),
    ],
)
def test_read_wraps_invalid_translation_frontmatter(
    tmp_path: Path, replacement: str
) -> None:
    text = "original"
    path = MarkdownStore(tmp_path).upsert(
        make_post(text=text, text_zh="译文", translation_zh=translation_for(text))
    )
    content = path.read_text(encoding="utf-8")
    start = content.index("translation_zh:")
    end = content.index("\n---\n", start)
    path.write_text(content[:start] + replacement + "\n" + content[end:], encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        MarkdownStore(tmp_path).read(path)

    assert str(raised.value) == f"Invalid import data in {path}: translation-metadata"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.parametrize(
    "body",
    [
        "<!-- xrag:text-zh:start -->\n译文",
        "译文\n<!-- xrag:text-zh:end -->",
        (
            "<!-- xrag:text-zh:start -->\na\n<!-- xrag:text-zh:start -->\nb\n"
            "<!-- xrag:text-zh:end -->"
        ),
        "<!-- xrag:text-zh:end -->\n译文\n<!-- xrag:text-zh:start -->",
        "<!-- xrag:text-zh:start -->\n\n<!-- xrag:text-zh:end -->",
        (
            "<!-- xrag:text:start -->\noriginal\n<!-- xrag:text-zh:start -->\n译文\n"
            "<!-- xrag:text:end -->\n<!-- xrag:text-zh:end -->"
        ),
    ],
)
def test_extract_body_translation_rejects_invalid_markers(body: str) -> None:
    with pytest.raises(ValueError, match="invalid canonical Markdown translation markers"):
        extract_body_translation(body)


def test_extract_body_translation_ignores_legacy_and_preserves_internal_formatting() -> None:
    legacy = "## 中文翻译（机器翻译）\nnot canonical"
    canonical = (
        "<!-- xrag:text:start -->\noriginal\n<!-- xrag:text:end -->\n"
        "<!-- xrag:text-zh:start -->\n第一行\n\n  indented\n<!-- xrag:text-zh:end -->"
    )

    assert extract_body_translation(legacy, canonical=False) == ""
    assert extract_body_translation("canonical without markers") == ""
    assert extract_body_translation(canonical) == "第一行\n\n  indented"


@pytest.mark.parametrize(
    "body",
    [
        "<!-- xrag:text:start -->\na\n<!-- xrag:text:start -->\nb\n<!-- xrag:text:end -->",
        "<!-- xrag:text:end -->\na\n<!-- xrag:text:start -->",
        (
            "<!-- xrag:text:start -->\noriginal\n<!-- xrag:text-zh:start -->\n译文\n"
            "<!-- xrag:text:end -->\n<!-- xrag:text-zh:end -->"
        ),
    ],
)
def test_extract_body_text_rejects_ambiguous_canonical_markers(body: str) -> None:
    with pytest.raises(ValueError, match="invalid canonical Markdown text markers"):
        extract_body_text(body)


def post_with_reserved_body(field: str, marker: str) -> Post:
    sensitive = f"SENSITIVE_BODY {marker} must-not-leak"
    if field == "post.text":
        return make_post(text=sensitive)
    if field == "post.text_zh":
        return make_post(
            text="original",
            text_zh=sensitive,
            translation_zh=translation_for("original"),
        )
    if field == "quoted.text":
        return make_post(
            quoted_post=QuotedPost(
                "456", "quoted", sensitive, "", "https://x.com/i/status/456"
            )
        )
    if field == "quoted.text_zh":
        quoted_text = "quoted original"
        return make_post(
            quoted_post=QuotedPost(
                "456",
                "quoted",
                quoted_text,
                "",
                "https://x.com/i/status/456",
                text_zh=sensitive,
                translation_zh=translation_for(quoted_text),
            )
        )
    raise AssertionError(field)


@pytest.mark.parametrize(
    "field, marker",
    [
        ("post.text", "<!-- xrag:text:start -->"),
        ("post.text_zh", "<!-- xrag:text:end -->"),
        ("quoted.text", "<!-- xrag:text-zh:start -->"),
        ("quoted.text_zh", "<!-- xrag:text-zh:end -->"),
    ],
)
def test_upsert_rejects_reserved_body_markers_before_any_write(
    tmp_path: Path, field: str, marker: str
) -> None:
    invalid = post_with_reserved_body(field, marker)
    new_directory = tmp_path / "new" / "posts"

    with pytest.raises(ValueError) as first_error:
        MarkdownStore(new_directory).upsert(invalid)

    assert str(first_error.value) == "post content contains reserved xrag Markdown marker"
    assert "SENSITIVE_BODY" not in str(first_error.value)
    assert not new_directory.exists()

    existing_directory = tmp_path / "existing"
    store = MarkdownStore(existing_directory)
    path = store.upsert(make_post(text="existing safe body"))
    before = path.read_bytes()

    with pytest.raises(ValueError) as update_error:
        store.upsert(invalid)

    assert str(update_error.value) == "post content contains reserved xrag Markdown marker"
    assert "SENSITIVE_BODY" not in str(update_error.value)
    assert path.read_bytes() == before
    assert list(existing_directory.glob("*.tmp")) == []


def test_legacy_body_treats_marker_literals_as_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "legacy.md"
    body = "before\n<!-- xrag:text:start -->\nmiddle\n<!-- xrag:text:end -->\nafter"
    path.write_text(
        """---
id: legacy
author: Ada
author_bio: ''
created_at: ''
collected_at: ''
updated_at: ''
url: https://x.com/i/status/legacy
likes: 0
views: 0
media_urls: []
source_keywords: []
source_type: opencli
---

""" + body + "\n",
        encoding="utf-8",
    )

    assert MarkdownStore(tmp_path).read(path).text == body


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

    with pytest.raises(ValueError) as raised:
        store.read(path)

    assert str(raised.value) == f"Invalid import data in {path}: front-matter"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


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

    with pytest.raises(ValueError) as raised:
        MarkdownStore(tmp_path).read(path)

    assert str(raised.value) == f"Invalid import data in {path}: front-matter"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_read_rejects_duplicate_canonical_front_matter_without_leaking_content(
    tmp_path: Path,
) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post())
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace("id: '123'\n", "id: '123'\nid: DUPLICATE_SECRET\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        store.read(path)

    assert str(raised.value) == f"Invalid import data in {path}: front-matter"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "DUPLICATE_SECRET" not in str(raised.value)


@pytest.mark.parametrize("field", ["likes", "views"])
def test_read_wraps_nonfinite_numeric_front_matter(field: str, tmp_path: Path) -> None:
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post())
    path.write_text(
        path.read_text(encoding="utf-8").replace(f"{field}: {5 if field == 'likes' else 100}", f"{field}: .inf", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as raised:
        store.read(path)

    assert str(raised.value) == f"Invalid import data in {path}: front-matter"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


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
