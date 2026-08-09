from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
import re

import yaml

from .models import Post


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_FRONT_MATTER_FIELDS = (
    "id",
    "author",
    "author_bio",
    "created_at",
    "collected_at",
    "updated_at",
    "url",
    "likes",
    "views",
    "media_urls",
    "source_keywords",
    "source_type",
)


class MarkdownStore:
    """A canonical, human-readable Markdown archive of collected X posts."""

    def __init__(self, directory: Path, *, clock: Callable[[], str] | None = None) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    def upsert(self, post: Post) -> Path:
        path = self._path_for(post.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        keywords = post.source_keywords
        collected_at = now
        if path.exists():
            metadata, _ = self._parse(path)
            keywords = _deduplicate((*_strings(metadata["source_keywords"]), *post.source_keywords))
            collected_at = str(metadata["collected_at"])

        normalized = Post(
            id=str(post.id),
            author=str(post.author),
            text=str(post.text).strip(),
            created_at=str(post.created_at),
            url=str(post.url),
            bio=str(post.bio),
            likes=int(post.likes),
            views=int(post.views),
            media_urls=_strings(post.media_urls),
            source_keywords=_deduplicate(keywords),
            source_type=str(post.source_type),
        )
        metadata = {
            "id": normalized.id,
            "author": normalized.author,
            "author_bio": normalized.bio,
            "created_at": normalized.created_at,
            "collected_at": collected_at,
            "updated_at": now,
            "url": normalized.url,
            "likes": normalized.likes,
            "views": normalized.views,
            "media_urls": list(normalized.media_urls),
            "source_keywords": list(normalized.source_keywords),
            "source_type": normalized.source_type,
        }
        content = "---\n" + yaml.safe_dump(
            metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
        ) + "---\n\n" + normalized.text + "\n"
        path.write_text(content, encoding="utf-8")
        return path

    def read(self, path: Path) -> Post:
        metadata, body = self._parse(Path(path))
        try:
            return Post(
                id=_scalar(metadata["id"], "id"),
                author=_scalar(metadata["author"], "author"),
                text=body.strip(),
                created_at=_scalar(metadata["created_at"], "created_at"),
                url=_scalar(metadata["url"], "url"),
                bio=_scalar(metadata["author_bio"], "author_bio"),
                likes=int(metadata["likes"]),
                views=int(metadata["views"]),
                media_urls=_strings(metadata["media_urls"]),
                source_keywords=_strings(metadata["source_keywords"]),
                source_type=_scalar(metadata["source_type"], "source_type"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid Markdown front matter in {path}: {error}") from error

    def iter_posts(self) -> Iterator[tuple[Path, Post]]:
        if not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.md")):
            yield path, self.read(path)

    def _path_for(self, post_id: str) -> Path:
        if not isinstance(post_id, str) or not _SAFE_ID.fullmatch(post_id):
            raise ValueError(f"unsafe post ID: {post_id!r}")
        path = self.directory / f"{post_id}.md"
        if path.resolve().parent != self.directory.resolve():
            raise ValueError(f"unsafe post ID: {post_id!r}")
        return path

    def _parse(self, path: Path) -> tuple[dict[str, object], str]:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"cannot read Markdown post {path}: {error}") from error
        if not content.startswith("---\n"):
            raise ValueError(f"invalid Markdown front matter in {path}: missing opening delimiter")
        end = content.find("\n---\n", 4)
        if end < 0:
            raise ValueError(f"invalid Markdown front matter in {path}: missing closing delimiter")
        try:
            metadata = yaml.safe_load(content[4:end])
        except yaml.YAMLError as error:
            raise ValueError(f"invalid Markdown front matter in {path}: {error}") from error
        if not isinstance(metadata, dict) or any(field not in metadata for field in _FRONT_MATTER_FIELDS):
            raise ValueError(f"invalid Markdown front matter in {path}: required fields are missing")
        return metadata, content[end + len("\n---\n") :].lstrip("\n")


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected a list of strings")
    if not all(isinstance(item, str) for item in value):
        raise TypeError("expected a list of strings")
    return tuple(value)


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _scalar(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value
