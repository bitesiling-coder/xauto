from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import tempfile

import yaml

from .models import LocalMedia, Post, QuotedPost


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
_TEXT_START = "<!-- xrag:text:start -->"
_TEXT_END = "<!-- xrag:text:end -->"


class MarkdownStore:
    """A canonical, human-readable Markdown archive of collected X posts."""

    def __init__(self, directory: Path, *, clock: Callable[[], str] | None = None) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    def upsert(self, post: Post) -> Path:
        path = self._path_for(post.id)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.validate_target(post.id)
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
            media_posters=_strings(post.media_posters),
            quoted_post=post.quoted_post,
            local_media=tuple(post.local_media),
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
            "media_posters": list(normalized.media_posters),
            "local_media": [_local_media_to_mapping(item) for item in normalized.local_media],
            "quoted_tweet": _quoted_to_mapping(normalized.quoted_post),
            "body_format": "xrag-v1",
            "source_keywords": list(normalized.source_keywords),
            "source_type": normalized.source_type,
        }
        content = "---\n" + yaml.safe_dump(
            metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
        ) + "---\n\n" + _render_body(normalized)
        self._write_atomic(path, content)
        return path

    def read(self, path: Path) -> Post:
        metadata, body = self._parse(Path(path))
        try:
            return Post(
                id=_scalar(metadata["id"], "id"),
                author=_scalar(metadata["author"], "author"),
                text=extract_body_text(body, canonical=_is_canonical_metadata(metadata)),
                created_at=_scalar(metadata["created_at"], "created_at"),
                url=_scalar(metadata["url"], "url"),
                bio=_scalar(metadata["author_bio"], "author_bio"),
                likes=int(metadata["likes"]),
                views=int(metadata["views"]),
                media_urls=_strings(metadata["media_urls"]),
                media_posters=_strings(metadata.get("media_posters", [])),
                quoted_post=_quoted_from_value(metadata.get("quoted_tweet")),
                local_media=_local_media_from_value(metadata.get("local_media", [])),
                source_keywords=_strings(metadata["source_keywords"]),
                source_type=_scalar(metadata["source_type"], "source_type"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid Markdown front matter in {path}: {error}") from error

    def get(self, post_id: str) -> Post | None:
        path = self._path_for(post_id)
        return self.read(path) if path.is_file() else None

    def validate_target(self, post_id: str) -> Path:
        path = self._path_for(post_id)
        self._ensure_no_casefold_collision(post_id)
        return path

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

    def _ensure_no_casefold_collision(self, post_id: str) -> None:
        for candidate in self.directory.glob("*.md"):
            if candidate.stem.casefold() == post_id.casefold() and candidate.stem != post_id:
                raise ValueError(f"case-insensitive collision for post ID: {post_id!r}")

    def _write_atomic(self, path: Path, content: str) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=self.directory,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

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


def extract_body_text(body: str, *, canonical: bool = True) -> str:
    if not canonical:
        return body.strip()
    has_start = _TEXT_START in body
    has_end = _TEXT_END in body
    if not has_start and not has_end:
        return body.strip()
    if not has_start or not has_end:
        raise ValueError("invalid canonical Markdown text markers")
    start = body.find(_TEXT_START)
    end = body.rfind(_TEXT_END)
    if end < start + len(_TEXT_START):
        raise ValueError("invalid canonical Markdown text markers")
    return body[start + len(_TEXT_START) : end].strip("\n")


def _render_body(post: Post) -> str:
    lines = [
        f"# @{post.author}的推文",
        "",
        "## 正文",
        "",
        _TEXT_START,
        post.text,
        _TEXT_END,
    ]
    top_media = [item for item in post.local_media if item.owner == "post"]
    if top_media or any(_is_video_url(url) for url in post.media_urls):
        lines.extend(["", "## 媒体", ""])
        lines.extend(_render_media(top_media, quoted=False))
        for url in post.media_urls:
            if _is_video_url(url):
                lines.extend([f"[打开原视频]({url})", ""])
        if lines[-1] == "":
            lines.pop()
    if post.quoted_post is not None:
        lines.extend(["", "## 引用推文", ""])
        quoted_lines = post.quoted_post.text.splitlines() or [""]
        lines.extend(
            [
                f"> @{post.quoted_post.author}：{quoted_lines[0]}",
                *[f"> {line}" for line in quoted_lines[1:]],
            ]
        )
        quoted_media = [item for item in post.local_media if item.owner == "quoted"]
        if quoted_media:
            lines.extend(["", *_render_media(quoted_media, quoted=True)])
        for url in post.quoted_post.media_urls:
            if _is_video_url(url):
                lines.extend(["", f"[打开引用原视频]({url})"])
        lines.extend(["", f"[查看引用推文]({post.quoted_post.url})"])
    lines.extend(["", f"[查看 X 原文]({post.url})", ""])
    return "\n".join(lines)


def _render_media(items: list[LocalMedia], *, quoted: bool) -> list[str]:
    lines: list[str] = []
    counts = {"image": 0, "video_poster": 0}
    for item in items:
        counts[item.kind] += 1
        if item.kind == "image":
            label = "引用图片" if quoted else "图片"
            remote_label = "查看引用原始图片" if quoted else "查看原始图片"
        else:
            label = "引用视频封面" if quoted else "视频封面"
            remote_label = "查看引用视频封面原图" if quoted else "查看视频封面原图"
        lines.extend(
            [
                f"![{label} {counts[item.kind]}]({item.relative_path})",
                "",
                f"[{remote_label}]({item.source_url})",
                "",
            ]
        )
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _is_video_url(url: str) -> bool:
    return url.lower().startswith("https://video.twimg.com/")


def _is_canonical_metadata(metadata: dict[str, object]) -> bool:
    return metadata.get("body_format") == "xrag-v1" or any(
        field in metadata for field in ("media_posters", "local_media", "quoted_tweet")
    )


def _quoted_to_mapping(value: QuotedPost | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "id": value.id,
        "author": value.author,
        "text": value.text,
        "created_at": value.created_at,
        "url": value.url,
        "media_urls": list(value.media_urls),
        "media_posters": list(value.media_posters),
    }


def _quoted_from_value(value: object) -> QuotedPost | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("quoted_tweet must be a mapping or null")
    try:
        return QuotedPost(
            id=_mapping_string(value, "id", "quoted_tweet"),
            author=_mapping_string(value, "author", "quoted_tweet"),
            text=_mapping_string(value, "text", "quoted_tweet"),
            created_at=_mapping_string(value, "created_at", "quoted_tweet"),
            url=_mapping_string(value, "url", "quoted_tweet"),
            media_urls=_strings(value.get("media_urls", [])),
            media_posters=_strings(value.get("media_posters", [])),
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid quoted_tweet: {error}") from error


def _local_media_to_mapping(value: LocalMedia) -> dict[str, str]:
    return {
        "owner": value.owner,
        "kind": value.kind,
        "source_url": value.source_url,
        "relative_path": value.relative_path,
        "content_type": value.content_type,
    }


def _local_media_from_value(value: object) -> tuple[LocalMedia, ...]:
    if not isinstance(value, list):
        raise ValueError("local_media must be a list")
    result: list[LocalMedia] = []
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("local_media entries must be mappings")
        owner = _mapping_string(row, "owner", "local_media")
        kind = _mapping_string(row, "kind", "local_media")
        if owner not in {"post", "quoted"}:
            raise ValueError("local_media owner is invalid")
        if kind not in {"image", "video_poster"}:
            raise ValueError("local_media kind is invalid")
        result.append(
            LocalMedia(
                owner=owner,  # type: ignore[arg-type]
                kind=kind,  # type: ignore[arg-type]
                source_url=_mapping_string(row, "source_url", "local_media"),
                relative_path=_mapping_string(row, "relative_path", "local_media"),
                content_type=_mapping_string(row, "content_type", "local_media"),
            )
        )
    return tuple(result)


def _mapping_string(value: dict[object, object], key: str, field: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ValueError(f"{field}.{key} must be a string")
    return item


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
