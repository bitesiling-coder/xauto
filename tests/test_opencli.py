from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.opencli import (
    OpenCLIClient,
    OpenCLIError,
    SearchBatch,
    SearchRejection,
    parse_search_yaml,
    parse_search_yaml_with_diagnostics,
)


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


def test_parse_search_yaml_reports_safe_diagnostics_for_each_malformed_row() -> None:
    batch = parse_search_yaml_with_diagnostics(
        """
- id: "123"
  text: safe text
- RECOGNIZABLE NONMAPPING SECRET
- text: RECOGNIZABLE MISSING ID BODY
- id: []
  text: RECOGNIZABLE INVALID ID BODY
- id: "456"
  text: "   "
  body: RECOGNIZABLE ARBITRARY VALUE
""",
        "AI",
    )

    assert isinstance(batch, SearchBatch)
    assert tuple(item.id for item in batch.posts) == ("123",)
    assert batch.rejections == (
        SearchRejection(1, "row[1]", "row is not a mapping"),
        SearchRejection(2, "row[2]", "missing or invalid id"),
        SearchRejection(3, "row[3]", "missing or invalid id"),
        SearchRejection(4, "456", "missing or blank text"),
    )
    diagnostics = repr(batch.rejections)
    assert "RECOGNIZABLE" not in diagnostics
    assert "BODY" not in diagnostics
    assert "SECRET" not in diagnostics
    assert "ARBITRARY" not in diagnostics


def test_parse_search_yaml_rejects_non_decimal_ids_without_exposing_them() -> None:
    batch = parse_search_yaml_with_diagnostics(
        """
- id: "auth_token=TOPSECRET RECOGNIZABLE BODY"
  text: safe
- id: -1
  text: safe
- id: true
  text: safe
- id: "１２３"
  text: safe
- id: " 123 "
  text: safe
- id: "00123"
  text: leading zeroes remain valid
- id: 0
  text: integer zero remains valid
""",
        "AI",
    )

    assert tuple(item.id for item in batch.posts) == ("00123", "0")
    assert batch.rejections == (
        SearchRejection(0, "row[0]", "missing or invalid id"),
        SearchRejection(1, "row[1]", "missing or invalid id"),
        SearchRejection(2, "row[2]", "missing or invalid id"),
        SearchRejection(3, "row[3]", "missing or invalid id"),
        SearchRejection(4, "row[4]", "missing or invalid id"),
    )
    diagnostics = repr(batch.rejections)
    assert "TOPSECRET" not in diagnostics
    assert "RECOGNIZABLE" not in diagnostics
    assert "BODY" not in diagnostics
    assert "auth_token" not in diagnostics


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


def test_client_search_and_search_batch_each_use_one_subprocess_invocation() -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='- id: "123"\n  text: okay\n- id: "456"\n',
            stderr="",
        )

    client = OpenCLIClient(run=run)
    assert [item.id for item in client.search("AI", 2)] == ["123"]
    assert len(calls) == 1

    batch = client.search_batch("GPU", 3)
    assert [item.id for item in batch.posts] == ["123"]
    assert batch.rejections == (
        SearchRejection(1, "456", "missing or blank text"),
    )
    assert len(calls) == 2


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
