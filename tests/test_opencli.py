from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.opencli import OpenCLIClient, OpenCLIError, parse_search_yaml


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "opencli-search.yaml"


def test_parse_search_yaml_normalizes_a_search_result() -> None:
    posts = parse_search_yaml(FIXTURE_PATH.read_text(encoding="utf-8"), "DDR5")

    assert len(posts) == 1
    post = posts[0]
    assert post.id == "2084640002085130466"
    assert post.author == "0xQiYan"
    assert post.text == "DDR5 内存价格上涨，装机成本又要增加了。"
    assert post.created_at == "2026-08-08T10:30:00Z"
    assert post.url == "https://x.com/0xQiYan/status/2084640002085130466"
    assert post.likes == 5
    assert post.views == 1739
    assert post.media_urls == ("https://pbs.twimg.com/media/DDR5-example.jpg",)
    assert post.source_keywords == ("DDR5",)


def test_parse_search_yaml_skips_malformed_rows_and_uses_fallbacks() -> None:
    posts = parse_search_yaml(
        """
- id: 42
  text: "  valid post  "
  author: 123
  bio: null
  created_at: null
  likes: "not a number"
  views: null
  media_urls: ["", 7, "https://example.com/image.jpg"]
- id: missing-text
- text: missing-id
- not-a-mapping
""",
        "AI",
    )

    assert len(posts) == 1
    post = posts[0]
    assert post.id == "42"
    assert post.author == "unknown"
    assert post.text == "valid post"
    assert post.bio == ""
    assert post.created_at == ""
    assert post.url == "https://x.com/i/status/42"
    assert post.likes == 0
    assert post.views == 0
    assert post.media_urls == ("https://example.com/image.jpg",)
    assert post.source_keywords == ("AI",)


def test_parse_search_yaml_rejects_a_non_list_root() -> None:
    with pytest.raises(OpenCLIError, match="list"):
        parse_search_yaml("id: 42", "AI")


def test_client_runs_the_expected_command_and_returns_empty_results() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="[]\n", stderr="")

    assert OpenCLIClient(run=run).search("AI", 12) == []
    assert calls == [
        (
            ["opencli", "twitter", "search", "AI", "--limit", "12", "-f", "yaml"],
            {"capture_output": True, "text": True, "encoding": "utf-8", "timeout": 180, "check": False},
        )
    ]


def test_client_raises_stderr_for_nonzero_results() -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="search unavailable")

    with pytest.raises(OpenCLIError, match="search unavailable"):
        OpenCLIClient(run=run).search("AI", 12)


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["opencli"], 180),
        FileNotFoundError("opencli not found"),
    ],
)
def test_client_wraps_expected_execution_failures(failure: Exception) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise failure

    with pytest.raises(OpenCLIError, match="OpenCLI search execution failed") as error:
        OpenCLIClient(run=run).search("AI", 12)

    assert error.value.__cause__ is failure


def test_parse_search_yaml_preserves_unquoted_iso_created_at() -> None:
    posts = parse_search_yaml(
        """
- id: "42"
  text: "valid"
  created_at: 2026-08-08T10:30:00Z
""",
        "AI",
    )

    assert posts[0].created_at.startswith("2026-08-08T10:30:00")


@pytest.mark.parametrize("post_id", ["[]", "{}", "true"])
def test_parse_search_yaml_skips_non_scalar_post_ids(post_id: str) -> None:
    assert parse_search_yaml(f"- id: {post_id}\n  text: valid\n", "AI") == []
