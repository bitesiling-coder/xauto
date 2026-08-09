from __future__ import annotations

from datetime import date
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.importers import load_posts


def test_load_posts_imports_yaml_and_json_lists(tmp_path: Path) -> None:
    yaml_path = tmp_path / "posts.yaml"
    json_path = tmp_path / "posts.json"
    yaml_path.write_text("- id: yaml-1\n  text: YAML post\n", encoding="utf-8")
    json_path.write_text(json.dumps([{"id": "json-1", "text": "JSON post"}]), encoding="utf-8")

    assert [post.id for post in load_posts(yaml_path)] == ["yaml-1"]
    assert [post.id for post in load_posts(json_path)] == ["json-1"]


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


def test_load_posts_rejects_unsupported_invalid_roots_and_rows(tmp_path: Path) -> None:
    unsupported = tmp_path / "posts.txt"
    invalid_root = tmp_path / "posts.yaml"
    invalid_row = tmp_path / "posts.json"
    unsupported.write_text("irrelevant", encoding="utf-8")
    invalid_root.write_text("just a string", encoding="utf-8")
    invalid_row.write_text('[{"id": "okay", "text": "valid"}, {"id": "bad"}]', encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported"):
        load_posts(unsupported)
    with pytest.raises(ValueError, match="mapping or list"):
        load_posts(invalid_root)
    with pytest.raises(ValueError, match="text"):
        load_posts(invalid_row)


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

    with pytest.raises(ValueError):
        load_posts(path)
