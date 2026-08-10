from xrag.models import Post, QuotedPost


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
