from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
import json
from pathlib import Path
import re

import yaml

from .markdown_store import (
    _is_canonical_metadata,
    _local_media_from_value,
    _quoted_from_value,
    _translation_from_value,
    _validate_translation_pair,
    extract_body_text,
    extract_body_translation,
)
from .models import Post


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def load_posts(path: Path) -> list[Post]:
    """Load and normalize posts from one YAML, JSON, or Markdown file."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in {".yaml", ".yml", ".json", ".md"}:
        raise ValueError(f"Unsupported import file type: {path.suffix or '(no extension)'}")
    try:
        if suffix in {".yaml", ".yml"}:
            rows = _load_yaml(path)
        elif suffix == ".json":
            rows = _load_json(path)
        else:
            rows = [_load_markdown(path)]

        normalized_rows = _rows(rows)
        posts = [_normalize_row(row) for row in normalized_rows]
        _validate_post_ids(posts)
        return posts
    except (
        OSError,
        RecursionError,
        TypeError,
        ValueError,
        yaml.YAMLError,
        json.JSONDecodeError,
    ):
        raise ValueError(f"Invalid import data in {path}") from None


def _load_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, RecursionError, yaml.YAMLError):
        raise ValueError(f"Cannot load YAML file {path}") from None


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RecursionError, json.JSONDecodeError):
        raise ValueError(f"Cannot load JSON file {path}") from None


def _load_markdown(path: Path) -> dict[str, object]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        raise ValueError(f"Cannot read Markdown file {path}") from None
    if not content.startswith("---\n"):
        raise ValueError(f"Invalid Markdown front matter in {path}: missing opening delimiter")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"Invalid Markdown front matter in {path}: missing closing delimiter")
    try:
        metadata = yaml.safe_load(content[4:end])
    except (RecursionError, yaml.YAMLError):
        raise ValueError(f"Invalid Markdown front matter in {path}") from None
    if not isinstance(metadata, Mapping):
        raise ValueError(f"Invalid Markdown front matter in {path}: expected a mapping")

    row = dict(metadata)
    if "id" not in row and _SAFE_ID.fullmatch(path.stem):
        row["id"] = path.stem
    body = content[end + len("\n---\n") :]
    try:
        row["text"] = extract_body_text(
            body,
            canonical=_is_canonical_metadata(row),
        )
        row["text_zh"] = extract_body_translation(
            body,
            canonical=_is_canonical_metadata(row),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid Markdown body in {path}") from error
    return row


def _rows(value: object) -> list[Mapping[object, object]]:
    if isinstance(value, Mapping):
        return [value]
    if not isinstance(value, list):
        raise ValueError("Import root must be a mapping or list of mappings")
    if not all(isinstance(row, Mapping) for row in value):
        raise ValueError("Each imported post must be a mapping")
    return value


def _normalize_row(row: Mapping[object, object]) -> Post:
    post_id = _identifier(row.get("id"))
    if not post_id:
        raise ValueError("Each imported post requires a nonempty id")
    text = _string(row.get("text"))
    if not text:
        raise ValueError("Each imported post requires nonempty text")
    translation_zh = _translation_from_value(row.get("translation_zh"))
    text_zh = _validate_translation_pair(
        text, row.get("text_zh", ""), translation_zh, "translation_zh"
    )

    return Post(
        id=post_id,
        author=_string(row.get("author")) or "unknown",
        text=text,
        created_at=_timestamp(row.get("created_at")),
        url=_string(row.get("url")) or f"https://x.com/i/status/{post_id}",
        bio=_string(row.get("author_bio")) or _string(row.get("bio")),
        likes=_nonnegative_integer(row.get("likes")),
        views=_nonnegative_integer(row.get("views")),
        media_urls=_strings(row.get("media_urls")),
        media_posters=_strings(row.get("media_posters")),
        quoted_post=_quoted_from_value(row.get("quoted_tweet")),
        local_media=_local_media_from_value(row.get("local_media", [])),
        source_keywords=_strings(row.get("source_keywords")),
        source_type="import",
        text_zh=text_zh,
        translation_zh=translation_zh,
    )


def _validate_post_ids(posts: list[Post]) -> None:
    seen: set[str] = set()
    for post in posts:
        if not _SAFE_ID.fullmatch(post.id):
            raise ValueError(f"unsafe post ID: {post.id!r}")
        folded_id = post.id.casefold()
        if folded_id in seen:
            raise ValueError(f"case-insensitive collision for post ID: {post.id!r}")
        seen.add(folded_id)


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


def _nonnegative_integer(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0) if value is not None else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
