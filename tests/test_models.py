from dataclasses import FrozenInstanceError

import pytest

from xrag.models import (
    Post,
    QuotedPost,
    TranslationMetadata,
    canonical_source_text,
    canonical_translation_text,
)


def test_canonical_source_text_normalizes_all_carriage_return_forms_before_strip() -> None:
    assert canonical_source_text(" \r\nfirst\rsecond\nthird\r\n ") == (
        "first\nsecond\nthird"
    )


def test_canonical_translation_text_preserves_non_newline_outer_whitespace() -> None:
    assert canonical_translation_text("\n  首行\r\n第二行  \r") == "  首行\n第二行  "


def test_searchable_text_includes_one_quoted_post_without_markdown_decoration() -> None:
    post = Post(
        id="123",
        author="main",
        text="main body",
        created_at="2026-08-10T00:00:00Z",
        url="https://x.com/main/status/123",
        quoted_post=QuotedPost(
            id="456",
            author="quoted",
            text="quoted body",
            created_at="2026-08-09T00:00:00Z",
            url="https://x.com/quoted/status/456",
        ),
    )

    assert post.searchable_text == "main body\n\nquoted body"


def test_searchable_text_orders_original_and_chinese_main_then_quoted() -> None:
    post = Post(
        id="123",
        author="main",
        text="  main original  ",
        created_at="2026-08-10T00:00:00Z",
        url="https://x.com/main/status/123",
        text_zh="  主帖中文  ",
        quoted_post=QuotedPost(
            id="456",
            author="quoted",
            text="  quoted original  ",
            created_at="2026-08-09T00:00:00Z",
            url="https://x.com/quoted/status/456",
            text_zh="  引用中文  ",
        ),
    )

    assert post.searchable_text == "main original\n\n主帖中文\n\nquoted original\n\n引用中文"


def test_searchable_text_includes_only_main_chinese_translation() -> None:
    post = Post(
        id="123",
        author="main",
        text="main original",
        created_at="2026-08-10T00:00:00Z",
        url="https://x.com/main/status/123",
        text_zh="主帖中文",
    )

    assert post.searchable_text == "main original\n\n主帖中文"


def test_searchable_text_without_translation_preserves_existing_result() -> None:
    post = Post(
        id="123",
        author="main",
        text="  main body  ",
        created_at="2026-08-10T00:00:00Z",
        url="https://x.com/main/status/123",
        quoted_post=QuotedPost(
            id="456",
            author="quoted",
            text="  quoted body  ",
            created_at="2026-08-09T00:00:00Z",
            url="https://x.com/quoted/status/456",
        ),
    )

    assert post.searchable_text == "main body\n\nquoted body"


def test_translation_metadata_is_frozen() -> None:
    metadata = TranslationMetadata(
        language="zh",
        model_id="model",
        revision="revision",
        source_sha256="sha256",
        translated_at="2026-08-10T00:00:00Z",
    )

    with pytest.raises(FrozenInstanceError):
        metadata.language = "en"
