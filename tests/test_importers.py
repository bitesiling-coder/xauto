from __future__ import annotations

from datetime import date
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.importers import load_posts
from xrag.markdown_store import MarkdownStore
from xrag.models import LocalMedia, Post, QuotedPost, TranslationMetadata, canonical_source_text


def translation_mapping(text: str, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "language": "zh-CN",
        "model_id": "translator-v1",
        "revision": "r1",
        "source_sha256": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
        "translated_at": "2026-08-10T00:00:00Z",
    }
    values.update(changes)
    return values


def assert_invalid_import(path: Path) -> ValueError:
    with pytest.raises(ValueError) as error:
        load_posts(path)
    assert str(error.value).startswith(f"Invalid import data in {path}: ")
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    return error.value


def exception_graph(error: BaseException) -> list[BaseException]:
    pending = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        pending.extend(
            child
            for child in (current.__cause__, current.__context__)
            if child is not None
        )
    return result


@pytest.mark.parametrize(
    ("filename", "content", "code", "sensitive"),
    [
        ("syntax.yaml", "text: [YAML_SECRET\n", "yaml-syntax", "YAML_SECRET"),
        ("syntax.json", '{"text":"JSON_SYNTAX_SECRET",}', "json-syntax", "JSON_SYNTAX_SECRET"),
        (
            "frontmatter.md",
            "---\nid: safe\ntext: [FRONTMATTER_SECRET\n---\nbody\n",
            "front-matter",
            "FRONTMATTER_SECRET",
        ),
        (
            "metadata.json",
            json.dumps(
                {
                    "id": "safe",
                    "text": "original",
                    "text_zh": "translation",
                    "translation_zh": "JSON_METADATA_SECRET",
                }
            ),
            "translation-metadata",
            "JSON_METADATA_SECRET",
        ),
        (
            "markers.md",
            "---\nid: safe\nbody_format: xrag-v1\n---\n"
            "<!-- xrag:text:start -->\noriginal\n<!-- xrag:text:end -->\n"
            "<!-- xrag:text-zh:start -->\nMARKER_SECRET\n",
            "markdown-markers",
            "MARKER_SECRET",
        ),
    ],
)
def test_load_posts_returns_a_detached_categorized_error_without_secrets(
    tmp_path: Path, filename: str, content: str, code: str, sensitive: str
) -> None:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_posts(path)

    error = raised.value
    assert str(error) == f"Invalid import data in {path}: {code}"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert all(sensitive not in str(item) for item in exception_graph(error))


@pytest.mark.parametrize(
    ("suffix", "code"),
    [
        (".yaml", "yaml-syntax"),
        (".json", "json-syntax"),
        (".md", "front-matter"),
    ],
)
def test_load_posts_classifies_invalid_utf8_at_the_format_boundary(
    tmp_path: Path, suffix: str, code: str
) -> None:
    path = tmp_path / f"bad{suffix}"
    path.write_bytes(b"\xff\xfe\x80")

    with pytest.raises(ValueError) as raised:
        load_posts(path)

    error = raised.value
    assert str(error) == f"Invalid import data in {path}: {code}"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert len(exception_graph(error)) == 1


@pytest.mark.parametrize(
    "content",
    [
        "id: DUPLICATE_TOP_SECRET\nid: second\ntext: valid\n",
        "text: DUPLICATE_TEXT_SECRET\ntext: second\nid: valid\n",
        "id: valid\ntext: original\ntext_zh: translation\ntranslation_zh:\n"
        "  language: zh-CN\n  language: DUPLICATE_LANGUAGE_SECRET\n",
        "id: valid\ntext: original\ntext_zh: translation\ntranslation_zh:\n"
        "  source_sha256: DUPLICATE_SOURCE_SECRET\n  'source_sha256': other\n",
        "id: valid\ntext: original\nquoted_tweet:\n  id: quote\n  author: A\n"
        "  text: quoted\n  created_at: ''\n  url: https://x.com/i/status/quote\n"
        "  media_urls: []\n  media_posters: []\n  translation_zh:\n"
        "    source_sha256: DUPLICATE_SHA_SECRET\n"
        "    'source_sha256': other\n",
        "id: valid\ntext: original\nquoted_tweet:\n  id: quote\n  author: A\n"
        "  text: quoted\n  created_at: ''\n  url: https://x.com/i/status/quote\n"
        "  media_urls: []\n  media_posters: []\n  translation_zh:\n"
        "    language: zh-CN\n    'language': DUPLICATE_QUOTED_LANGUAGE_SECRET\n",
    ],
)
def test_load_posts_rejects_duplicate_yaml_keys_at_every_mapping_depth(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        load_posts(path)

    assert str(raised.value) == f"Invalid import data in {path}: yaml-syntax"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "SECRET" not in str(raised.value)


def test_load_posts_imports_yaml_and_json_lists(tmp_path: Path) -> None:
    yaml_path = tmp_path / "posts.yaml"
    json_path = tmp_path / "posts.json"
    yaml_path.write_text("- id: yaml-1\n  text: YAML post\n", encoding="utf-8")
    json_path.write_text(json.dumps([{"id": "json-1", "text": "JSON post"}]), encoding="utf-8")

    assert [post.id for post in load_posts(yaml_path)] == ["yaml-1"]
    assert [post.id for post in load_posts(json_path)] == ["json-1"]


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_load_posts_imports_main_and_quoted_translations(
    tmp_path: Path, suffix: str
) -> None:
    text = "original\nmultiline"
    quoted_text = "quoted original"
    row = {
        "id": "translated",
        "text": text,
        "text_zh": "第一行\n\n第二行",
        "translation_zh": translation_mapping(text),
        "quoted_tweet": {
            "id": "quote",
            "author": "Bob",
            "text": quoted_text,
            "created_at": "",
            "url": "https://x.com/i/status/quote",
            "media_urls": [],
            "media_posters": [],
            "text_zh": "引用译文",
            "translation_zh": translation_mapping(quoted_text),
        },
    }
    path = tmp_path / f"translated{suffix}"
    if suffix == ".json":
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(row, allow_unicode=True), encoding="utf-8")

    [post] = load_posts(path)

    assert post.text == text
    assert post.text_zh == "第一行\n\n第二行"
    assert post.translation_zh == TranslationMetadata(**translation_mapping(text))
    assert post.quoted_post is not None
    assert post.quoted_post.text_zh == "引用译文"
    assert post.quoted_post.translation_zh == TranslationMetadata(
        **translation_mapping(quoted_text)
    )


def test_load_posts_round_trips_canonical_bilingual_markdown(tmp_path: Path) -> None:
    text = " exact original\nsecond line "
    metadata = TranslationMetadata(**translation_mapping(text))
    original = Post(
        "123", "Ada", text, "", "https://x.com/i/status/123",
        text_zh="译文第一行\n\n译文第三行", translation_zh=metadata,
    )
    path = MarkdownStore(tmp_path).upsert(original)

    [post] = load_posts(path)

    assert post.text == text.strip()
    assert post.text_zh == original.text_zh
    assert post.translation_zh == metadata


def test_json_import_with_cr_newlines_writes_and_reads_canonical_markdown(
    tmp_path: Path,
) -> None:
    main_text = " Main English source\r\nwith a lone\rreturn "
    quoted_text = " Quoted English source\rwith another\r\nreturn "
    row = {
        "id": "cr-json",
        "text": main_text,
        "text_zh": " 主帖译文\r\n第二行 ",
        "translation_zh": translation_mapping(canonical_source_text(main_text)),
        "quoted_tweet": {
            "id": "quote",
            "author": "quoted",
            "text": quoted_text,
            "created_at": "",
            "url": "https://x.com/i/status/quote",
            "media_urls": [],
            "media_posters": [],
            "text_zh": " 引用译文\r第二行 ",
            "translation_zh": translation_mapping(canonical_source_text(quoted_text)),
        },
    }
    source = tmp_path / "cr.json"
    source.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    [imported] = load_posts(source)
    path = MarkdownStore(tmp_path).upsert(imported)
    reread = MarkdownStore(tmp_path).read(path)

    assert "\r" not in path.read_text(encoding="utf-8")
    assert reread.text == canonical_source_text(main_text)
    assert reread.text_zh == canonical_source_text(row["text_zh"])
    assert reread.quoted_post is not None
    assert reread.quoted_post.text == canonical_source_text(quoted_text)
    assert reread.quoted_post.text_zh == canonical_source_text(
        row["quoted_tweet"]["text_zh"]
    )


def test_load_posts_does_not_infer_translation_from_noncanonical_heading(
    tmp_path: Path,
) -> None:
    path = tmp_path / "external.md"
    path.write_text(
        "---\nid: external\n---\nOriginal\n\n## 中文翻译（机器翻译）\nNot metadata\n",
        encoding="utf-8",
    )

    [post] = load_posts(path)

    assert post.text_zh == ""
    assert post.translation_zh is None


@pytest.mark.parametrize(
    "row",
    [
        {"id": "post", "text": "original", "text_zh": "译文"},
        {
            "id": "post",
            "text": "original",
            "translation_zh": translation_mapping("original"),
        },
        {
            "id": "post",
            "text": "original",
            "text_zh": "译文",
            "translation_zh": translation_mapping("different"),
        },
        {
            "id": "post",
            "text": "original",
            "text_zh": "译文",
            "translation_zh": {**translation_mapping("original"), "extra": "no"},
        },
        {
            "id": "post",
            "text": "original",
            "text_zh": "译文",
            "translation_zh": [],
        },
    ],
)
def test_load_posts_rejects_invalid_translation_pairs(
    tmp_path: Path, row: dict[str, object]
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")

    error = assert_invalid_import(path)
    assert "SENSITIVE" not in str(error)


@pytest.mark.parametrize("suffix", [".yaml", ".json"])
def test_load_posts_wraps_bilingual_metadata_errors_without_leaking_payload(
    tmp_path: Path, suffix: str
) -> None:
    sensitive = "SENSITIVE_METADATA_PAYLOAD"
    if suffix == ".json":
        row: object = {
            "id": "post",
            "text": "original",
            "text_zh": "译文",
            "translation_zh": sensitive,
        }
    else:
        row = {
            "id": "post",
            "text": "original",
            "quoted_tweet": sensitive,
        }
    path = tmp_path / f"invalid{suffix}"
    if suffix == ".json":
        path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(row, allow_unicode=True), encoding="utf-8")
    before = path.read_bytes()

    error = assert_invalid_import(path)
    assert sensitive not in str(error)
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.md")) == []


def test_load_posts_wraps_markdown_body_errors_without_leaking_payload(
    tmp_path: Path,
) -> None:
    sensitive = "SENSITIVE_MARKDOWN_PAYLOAD"
    path = tmp_path / "invalid.md"
    path.write_text(
        "---\nid: post\nbody_format: xrag-v1\n---\n"
        "<!-- xrag:text:start -->\noriginal\n<!-- xrag:text:end -->\n"
        f"<!-- xrag:text-zh:start -->\n{sensitive}\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    before_names = sorted(item.name for item in tmp_path.iterdir())

    error = assert_invalid_import(path)
    assert sensitive not in str(error)
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == before_names


@pytest.mark.parametrize(
    "filename, content, code",
    [
        ("invalid.yaml", "post: [SENSITIVE_YAML_SYNTAX\n", "yaml-syntax"),
        (
            "invalid.md",
            "---\npost: [SENSITIVE_MARKDOWN_FRONTMATTER\n---\nbody\n",
            "front-matter",
        ),
    ],
)
def test_load_posts_sanitizes_yaml_parser_errors(
    tmp_path: Path, filename: str, content: str, code: str
) -> None:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_posts(path)

    assert str(error.value) == f"Invalid import data in {path}: {code}"
    assert "SENSITIVE" not in str(error.value)
    assert "while parsing" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "filename, content, code",
    [
        ("scalar.yaml", "SENSITIVE_SCALAR_ROOT\n", "document-shape"),
        (
            "rows.json",
            '[{"id":"valid","text":"valid"},"SENSITIVE_NONMAPPING_ROW"]',
            "document-shape",
        ),
        (
            "unsafe.json",
            '{"id":"../SENSITIVE_UNSAFE_ID","text":"valid"}',
            "post-id",
        ),
    ],
)
def test_load_posts_sanitizes_shape_and_identifier_errors(
    tmp_path: Path, filename: str, content: str, code: str
) -> None:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_posts(path)

    assert str(error.value) == f"Invalid import data in {path}: {code}"
    assert "SENSITIVE" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize("suffix, content", [
    (".yml", "id: only-yml\ntext: a YAML mapping\n"),
    (".json", '{"id": "only-json", "text": "a JSON object"}'),
])
def test_load_posts_accepts_a_single_mapping_root(tmp_path: Path, suffix: str, content: str) -> None:
    path = tmp_path / f"post{suffix}"
    path.write_text(content, encoding="utf-8")

    assert [post.id for post in load_posts(path)] == [f"only-{suffix[1:]}"]


def test_load_posts_imports_markdown_front_matter_and_body(tmp_path: Path) -> None:
    path = tmp_path / "hello.md"
    path.write_text(
        "---\nid: markdown-1\nauthor: Ada\ncreated_at: 2026-08-08\n---\nMarkdown body\n",
        encoding="utf-8",
    )

    [post] = load_posts(path)

    assert post.id == "markdown-1"
    assert post.text == "Markdown body"
    assert post.created_at == "2026-08-08"
    assert post.source_type == "import"


def test_load_posts_extracts_only_canonical_marked_text(tmp_path: Path) -> None:
    source = tmp_path / "canonical"
    path = MarkdownStore(source, clock=lambda: "2026-08-09T00:00:00Z").upsert(
        Post(
            "123",
            "Ada",
            "正文内容",
            "",
            "https://x.com/i/status/123",
            media_posters=("https://pbs.twimg.com/media/poster",),
            quoted_post=QuotedPost(
                "456", "Bob", "引用内容", "", "https://x.com/i/status/456"
            ),
            local_media=(
                LocalMedia(
                    "post",
                    "video_poster",
                    "https://pbs.twimg.com/media/poster",
                    "../media/123/video-poster-01.jpg",
                    "image/jpeg",
                ),
            ),
        )
    )

    [post] = load_posts(path)

    assert post.text == "正文内容"
    assert "## 正文" not in post.text
    assert "![视频封面" not in post.text
    assert post.media_posters == ("https://pbs.twimg.com/media/poster",)
    assert post.quoted_post is not None
    assert post.quoted_post.text == "引用内容"
    assert post.local_media[0].relative_path == "../media/123/video-poster-01.jpg"


def test_load_posts_rejects_unsupported_invalid_roots_and_rows(tmp_path: Path) -> None:
    unsupported = tmp_path / "posts.txt"
    invalid_root = tmp_path / "posts.yaml"
    invalid_row = tmp_path / "posts.json"
    unsupported.write_text("irrelevant", encoding="utf-8")
    invalid_root.write_text("just a string", encoding="utf-8")
    invalid_row.write_text('[{"id": "okay", "text": "valid"}, {"id": "bad"}]', encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported"):
        load_posts(unsupported)
    assert_invalid_import(invalid_root)
    assert_invalid_import(invalid_row)


def test_load_posts_normalizes_scalars_numbers_and_dates(tmp_path: Path) -> None:
    path = tmp_path / "post.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": 42,
                "text": " valid ",
                "bio": " profile ",
                "created_at": date(2026, 8, 8),
                "likes": "invalid",
                "views": -3,
                "media_urls": " https://example.com/image.jpg ",
                "source_keywords": " AI ",
            }
        ),
        encoding="utf-8",
    )

    [post] = load_posts(path)

    assert post.id == "42"
    assert post.author == "unknown"
    assert post.bio == "profile"
    assert post.created_at == "2026-08-08"
    assert post.url == "https://x.com/i/status/42"
    assert post.likes == 0
    assert post.views == 0
    assert post.media_urls == ("https://example.com/image.jpg",)
    assert post.source_keywords == ("AI",)


@pytest.mark.parametrize(
    "content",
    [
        "id: missing-front-matter\n---\nbody\n",
        "---\nid: missing-text\n---\n",
    ],
)
def test_load_posts_rejects_malformed_markdown_or_missing_text(tmp_path: Path, content: str) -> None:
    path = tmp_path / "post.md"
    path.write_text(content, encoding="utf-8")

    assert_invalid_import(path)


def test_markdown_uses_a_safe_stem_only_when_id_is_missing(tmp_path: Path) -> None:
    fallback = tmp_path / "safe-id.md"
    explicit = tmp_path / "safe-id-2.md"
    unsafe = tmp_path / "unsafe.stem.md"
    fallback.write_text("---\nauthor: Ada\n---\nbody\n", encoding="utf-8")
    explicit.write_text("---\nid: explicit-id\n---\nbody\n", encoding="utf-8")
    unsafe.write_text("---\nauthor: Ada\n---\nbody\n", encoding="utf-8")

    assert load_posts(fallback)[0].id == "safe-id"
    assert load_posts(explicit)[0].id == "explicit-id"
    assert_invalid_import(unsafe)


@pytest.mark.parametrize("post_id", ["null", "''", "'   '"])
def test_markdown_does_not_use_stem_when_invalid_id_is_explicit(
    tmp_path: Path, post_id: str
) -> None:
    path = tmp_path / "safe-stem.md"
    path.write_text(f"---\nid: {post_id}\n---\nbody\n", encoding="utf-8")

    assert_invalid_import(path)


@pytest.mark.parametrize(
    "content",
    [
        "---\nid: post\ntext: ignored\n",
        "---\n- not\n- mapping\n---\nbody\n",
    ],
)
def test_load_posts_rejects_invalid_markdown_front_matter(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "post.md"
    path.write_text(content, encoding="utf-8")

    assert_invalid_import(path)


def test_load_posts_rejects_a_non_mapping_list_row(tmp_path: Path) -> None:
    path = tmp_path / "posts.yaml"
    path.write_text("- id: valid\n  text: valid\n- invalid\n", encoding="utf-8")

    assert_invalid_import(path)


@pytest.mark.parametrize("post_id", ["", "   ", True, 1.5])
def test_load_posts_rejects_invalid_post_ids(tmp_path: Path, post_id: object) -> None:
    path = tmp_path / "post.yaml"
    path.write_text(yaml.safe_dump({"id": post_id, "text": "valid"}), encoding="utf-8")

    assert_invalid_import(path)


def test_load_posts_accepts_integer_id_and_author_bio_alias(tmp_path: Path) -> None:
    path = tmp_path / "post.yaml"
    path.write_text("id: 7\ntext: valid\nauthor_bio: profile\n", encoding="utf-8")

    [post] = load_posts(path)

    assert post.id == "7"
    assert post.bio == "profile"


def test_load_posts_preserves_datetime_lists_numbers_and_utf8_text(tmp_path: Path) -> None:
    path = tmp_path / "post.yaml"
    path.write_text(
        """id: chinese-post
text: 中文内容
created_at: 2026-08-08T10:30:00Z
likes: 5
views: 1739
media_urls: ["", 7, " https://example.com/one.jpg ", null, "https://example.com/two.jpg"]
source_keywords: ["", 7, " AI ", null, "GPU"]
""",
        encoding="utf-8",
    )

    [post] = load_posts(path)

    assert post.text == "中文内容"
    assert post.created_at.startswith("2026-08-08T10:30:00")
    assert post.likes == 5
    assert post.views == 1739
    assert post.media_urls == ("https://example.com/one.jpg", "https://example.com/two.jpg")
    assert post.source_keywords == ("AI", "GPU")


def test_load_posts_does_not_write_or_recurse(tmp_path: Path) -> None:
    source = tmp_path / "source.yaml"
    nested = tmp_path / "nested"
    source.write_bytes("id: original\ntext: unchanged\n".encode("utf-8"))
    nested.mkdir()
    (nested / "inside.yaml").write_text("id: nested\ntext: ignored\n", encoding="utf-8")
    before = source.read_bytes()

    assert load_posts(source)[0].id == "original"
    assert source.read_bytes() == before
    with pytest.raises(ValueError, match="Unsupported"):
        load_posts(nested)


def test_load_posts_rejects_an_unsafe_id_in_a_batch_before_returning_posts(tmp_path: Path) -> None:
    path = tmp_path / "posts.yaml"
    path.write_text(
        "- id: valid-id\n  text: valid\n- id: ../invalid\n  text: invalid\n",
        encoding="utf-8",
    )

    assert_invalid_import(path)


def test_load_posts_rejects_casefold_duplicate_ids_in_a_batch(tmp_path: Path) -> None:
    path = tmp_path / "posts.yaml"
    path.write_text(
        "- id: abc\n  text: first\n- id: ABC\n  text: second\n",
        encoding="utf-8",
    )

    assert_invalid_import(path)


def test_load_posts_wraps_recursion_errors_from_deep_yaml(tmp_path: Path) -> None:
    depth = sys.getrecursionlimit() + 100
    path = tmp_path / "deep.yaml"
    path.write_text("value: " + "[" * depth + "value" + "]" * depth, encoding="utf-8")

    assert_invalid_import(path)


def test_load_posts_wraps_recursion_errors_from_deep_json(tmp_path: Path) -> None:
    depth = 100_000
    path = tmp_path / "deep.json"
    path.write_text("[" * depth + '"value"' + "]" * depth, encoding="utf-8")

    assert_invalid_import(path)


def test_load_posts_wraps_recursion_errors_from_deep_markdown_front_matter(tmp_path: Path) -> None:
    depth = sys.getrecursionlimit() + 100
    path = tmp_path / "deep.md"
    path.write_text(
        "---\nvalue: " + "[" * depth + "value" + "]" * depth + "\n---\nbody\n",
        encoding="utf-8",
    )

    assert_invalid_import(path)
