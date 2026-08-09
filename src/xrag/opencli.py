from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
import subprocess

import yaml

from xrag.models import Post


class OpenCLIError(RuntimeError):
    pass


class OpenCLIClient:
    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 180,
    ) -> None:
        self._run = run
        self._timeout = timeout

    def search(self, keyword: str, limit: int) -> list[Post]:
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
        return parse_search_yaml(result.stdout, keyword)


def parse_search_yaml(payload: str, keyword: str) -> list[Post]:
    try:
        rows = yaml.safe_load(payload)
    except yaml.YAMLError as error:
        raise OpenCLIError("Invalid YAML from opencli") from error

    if not isinstance(rows, list):
        raise OpenCLIError("OpenCLI search result must be a list")

    posts: list[Post] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        post = _normalize_post(row, keyword)
        if post is not None:
            posts.append(post)
    return posts


def _normalize_post(row: dict[object, object], keyword: str) -> Post | None:
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
        source_keywords=(keyword,),
    )


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _identifier(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    return str(value) if type(value) is int else ""


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
