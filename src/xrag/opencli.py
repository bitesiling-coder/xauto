from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
import subprocess

import yaml

from xrag.models import Post, QuotedPost


class OpenCLIError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SearchRejection:
    index: int
    identifier: str
    reason: str


@dataclass(frozen=True, slots=True)
class SearchBatch:
    posts: tuple[Post, ...]
    rejections: tuple[SearchRejection, ...]


class OpenCLIClient:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 180,
    ) -> None:
        self._run = run
        self._timeout = timeout

    def search(self, keyword: str, limit: int) -> list[Post]:
        return list(self.search_batch(keyword, limit).posts)

    def search_batch(self, keyword: str, limit: int) -> SearchBatch:
        command = [
            "opencli",
            "twitter",
            "search",
            keyword,
            "--limit",
            str(limit),
            "-f",
            "yaml",
        ]
        try:
            result = self._run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError, UnicodeError) as error:
            raise OpenCLIError(f"OpenCLI search execution failed: {error}") from error
        if result.returncode != 0:
            message = result.stderr or result.stdout or "opencli search failed"
            raise OpenCLIError(message.strip())
        return parse_search_yaml_with_diagnostics(result.stdout, keyword)


def parse_search_yaml(payload: str, keyword: str) -> list[Post]:
    return list(parse_search_yaml_with_diagnostics(payload, keyword).posts)


def parse_search_yaml_with_diagnostics(payload: str, keyword: str) -> SearchBatch:
    try:
        rows = yaml.safe_load(payload)
    except yaml.YAMLError as error:
        raise OpenCLIError("Invalid YAML from opencli") from error

    if not isinstance(rows, list):
        raise OpenCLIError("OpenCLI search result must be a list")

    posts: list[Post] = []
    rejections: list[SearchRejection] = []
    for index, row in enumerate(rows):
        fallback = f"row[{index}]"
        if not isinstance(row, Mapping):
            rejections.append(
                SearchRejection(index, fallback, "row is not a mapping")
            )
            continue
        post_id = _identifier(row.get("id"))
        if not post_id:
            rejections.append(
                SearchRejection(index, fallback, "missing or invalid id")
            )
            continue
        if not _string(row.get("text")):
            rejections.append(
                SearchRejection(index, post_id, "missing or blank text")
            )
            continue
        post = _normalize_post(row, keyword)
        assert post is not None
        posts.append(post)
    return SearchBatch(tuple(posts), tuple(rejections))


def _normalize_post(row: Mapping[object, object], keyword: str) -> Post | None:
    post_id = _identifier(row.get("id"))
    text = _string(row.get("text"))
    if not post_id or not text:
        return None

    author = _string(row.get("author")) or "unknown"
    url = _string(row.get("url")) or f"https://x.com/i/status/{post_id}"
    return Post(
        id=post_id,
        author=author,
        text=text,
        created_at=_timestamp(row.get("created_at")),
        url=url,
        bio=_string(row.get("bio")),
        likes=_integer(row.get("likes")),
        views=_integer(row.get("views")),
        media_urls=_media_urls(row.get("media_urls")),
        media_posters=_media_urls(row.get("media_posters")),
        quoted_post=_quoted_post(row.get("quoted_tweet")),
        source_keywords=(keyword,),
    )


def _quoted_post(value: object) -> QuotedPost | None:
    if not isinstance(value, Mapping):
        return None
    post_id = _identifier(value.get("id"))
    text = _string(value.get("text"))
    if not post_id or not text:
        return None
    return QuotedPost(
        id=post_id,
        author=_string(value.get("author")) or "unknown",
        text=text,
        created_at=_timestamp(value.get("created_at")),
        url=_string(value.get("url")) or f"https://x.com/i/status/{post_id}",
        media_urls=_media_urls(value.get("media_urls")),
        media_posters=_media_urls(value.get("media_posters")),
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identifier(value: object) -> str:
    if isinstance(value, str):
        return (
            value
            if value and all("0" <= character <= "9" for character in value)
            else ""
        )
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return ""


def _timestamp(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _media_urls(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
