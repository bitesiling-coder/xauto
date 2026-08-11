from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile

import yaml

from .models import LocalMedia, Post, QuotedPost, TranslationMetadata


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
_TEXT_ZH_START = "<!-- xrag:text-zh:start -->"
_TEXT_ZH_END = "<!-- xrag:text-zh:end -->"
_RESERVED_BODY_MARKERS = (_TEXT_START, _TEXT_END, _TEXT_ZH_START, _TEXT_ZH_END)
_TRANSLATION_FIELDS = {
    "language",
    "model_id",
    "revision",
    "source_sha256",
    "translated_at",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def safe_load_unique(content: str) -> object:
    return yaml.load(content, Loader=_UniqueKeyLoader)


class _MarkdownProblem(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class MarkdownStore:
    """A canonical, human-readable Markdown archive of collected X posts."""

    def __init__(self, directory: Path, *, clock: Callable[[], str] | None = None) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    def upsert(self, post: Post) -> Path:
        body_values: list[object] = [post.text, post.text_zh]
        if post.quoted_post is not None:
            body_values.extend([post.quoted_post.text, post.quoted_post.text_zh])
        _reject_reserved_body_markers(body_values)
        text_zh = _validate_translation_pair(
            post.text, post.text_zh, post.translation_zh, "translation_zh"
        )
        quoted_post = post.quoted_post
        if post.quoted_post is not None:
            quoted_text_zh = _validate_translation_pair(
                post.quoted_post.text,
                post.quoted_post.text_zh,
                post.quoted_post.translation_zh,
                "quoted_tweet.translation_zh",
            )
            quoted_post = QuotedPost(
                id=post.quoted_post.id,
                author=post.quoted_post.author,
                text=post.quoted_post.text,
                created_at=post.quoted_post.created_at,
                url=post.quoted_post.url,
                media_urls=post.quoted_post.media_urls,
                media_posters=post.quoted_post.media_posters,
                text_zh=quoted_text_zh,
                translation_zh=post.quoted_post.translation_zh,
            )
        path = self._path_for(post.id)
        self.validate_target(post.id)
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
            media_posters=_strings(post.media_posters),
            quoted_post=quoted_post,
            local_media=tuple(post.local_media),
            source_keywords=_deduplicate(keywords),
            source_type=str(post.source_type),
            text_zh=text_zh,
            translation_zh=post.translation_zh,
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
        if normalized.translation_zh is not None:
            metadata["translation_zh"] = _translation_to_mapping(
                normalized.translation_zh
            )
        content = "---\n" + yaml.safe_dump(
            metadata, allow_unicode=True, sort_keys=False, default_flow_style=False
        ) + "---\n\n" + _render_body(normalized)
        self._write_atomic(path, content)
        return path

    def read(self, path: Path) -> Post:
        path = Path(path)
        problem: _MarkdownProblem | None = None
        try:
            metadata, body = self._parse(path)
            canonical = _is_canonical_metadata(metadata)
            try:
                text = extract_body_text(body, canonical=canonical)
                text_zh = extract_body_translation(body, canonical=canonical)
            except ValueError:
                raise _MarkdownProblem("markdown-markers")
            try:
                translation_zh = _translation_from_value(metadata.get("translation_zh"))
                text_zh = _validate_translation_pair(
                    text, text_zh, translation_zh, "translation_zh"
                )
                quoted_post = _quoted_from_value(metadata.get("quoted_tweet"))
            except (TypeError, ValueError):
                raise _MarkdownProblem("translation-metadata")
            return Post(
                id=_scalar(metadata["id"], "id"),
                author=_scalar(metadata["author"], "author"),
                text=text,
                created_at=_scalar(metadata["created_at"], "created_at"),
                url=_scalar(metadata["url"], "url"),
                bio=_scalar(metadata["author_bio"], "author_bio"),
                likes=int(metadata["likes"]),
                views=int(metadata["views"]),
                media_urls=_strings(metadata["media_urls"]),
                media_posters=_strings(metadata.get("media_posters", [])),
                quoted_post=quoted_post,
                local_media=_local_media_from_value(metadata.get("local_media", [])),
                source_keywords=_strings(metadata["source_keywords"]),
                source_type=_scalar(metadata["source_type"], "source_type"),
                text_zh=text_zh,
                translation_zh=translation_zh,
            )
        except _MarkdownProblem as error:
            problem = error
        except (KeyError, OverflowError, TypeError, ValueError, RecursionError):
            problem = _MarkdownProblem("front-matter")
        if problem is not None:
            raise ValueError(f"Invalid import data in {path}: {problem.code}")

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
        except OSError:
            raise _MarkdownProblem("front-matter")
        if not content.startswith("---\n"):
            raise _MarkdownProblem("front-matter")
        end = content.find("\n---\n", 4)
        if end < 0:
            raise _MarkdownProblem("front-matter")
        try:
            metadata = safe_load_unique(content[4:end])
        except (RecursionError, yaml.YAMLError):
            raise _MarkdownProblem("front-matter")
        if not isinstance(metadata, dict) or any(field not in metadata for field in _FRONT_MATTER_FIELDS):
            raise _MarkdownProblem("front-matter")
        return metadata, content[end + len("\n---\n") :].lstrip("\n")


def extract_body_text(body: str, *, canonical: bool = True) -> str:
    if not canonical:
        return body.strip()
    start_count = body.count(_TEXT_START)
    end_count = body.count(_TEXT_END)
    if start_count == 0 and end_count == 0:
        if _TEXT_ZH_START in body or _TEXT_ZH_END in body:
            raise ValueError("invalid canonical Markdown text markers")
        return body.strip()
    if start_count != 1 or end_count != 1:
        raise ValueError("invalid canonical Markdown text markers")
    start = body.find(_TEXT_START)
    end = body.find(_TEXT_END)
    if end < start + len(_TEXT_START):
        raise ValueError("invalid canonical Markdown text markers")
    if _TEXT_ZH_START in body or _TEXT_ZH_END in body:
        try:
            extract_body_translation(body, canonical=True)
        except ValueError as error:
            raise ValueError("invalid canonical Markdown text markers") from error
    return body[start + len(_TEXT_START) : end].strip("\n")


def extract_body_translation(body: str, *, canonical: bool = True) -> str:
    if not canonical:
        return ""
    start_count = body.count(_TEXT_ZH_START)
    end_count = body.count(_TEXT_ZH_END)
    if start_count == 0 and end_count == 0:
        return ""
    if start_count != 1 or end_count != 1:
        raise ValueError("invalid canonical Markdown translation markers")
    start = body.find(_TEXT_ZH_START)
    end = body.find(_TEXT_ZH_END)
    if end < start + len(_TEXT_ZH_START):
        raise ValueError("invalid canonical Markdown translation markers")
    if body.count(_TEXT_START) != 1 or body.count(_TEXT_END) != 1:
        raise ValueError("invalid canonical Markdown translation markers")
    original_start = body.find(_TEXT_START)
    original_end = body.find(_TEXT_END)
    if (
        original_end < original_start + len(_TEXT_START)
        or original_end + len(_TEXT_END) > start
    ):
        raise ValueError("invalid canonical Markdown translation markers")
    text_zh = body[start + len(_TEXT_ZH_START) : end].strip("\n")
    if not text_zh.strip():
        raise ValueError("invalid canonical Markdown translation markers")
    return text_zh


def _reject_reserved_body_markers(values: list[object]) -> None:
    for value in values:
        if isinstance(value, str) and any(
            marker in value for marker in _RESERVED_BODY_MARKERS
        ):
            raise ValueError("post content contains reserved xrag Markdown marker")


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
    if post.translation_zh is not None:
        lines.extend(
            [
                "",
                "## 中文翻译（机器翻译）",
                "",
                _TEXT_ZH_START,
                post.text_zh,
                _TEXT_ZH_END,
            ]
        )
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
        if post.quoted_post.translation_zh is not None:
            lines.extend(["", "> **中文翻译（机器翻译）**"])
            for line in post.quoted_post.text_zh.splitlines() or [""]:
                lines.append(f"> {line}" if line else ">")
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
    mapping: dict[str, object] = {
        "id": value.id,
        "author": value.author,
        "text": value.text,
        "created_at": value.created_at,
        "url": value.url,
        "media_urls": list(value.media_urls),
        "media_posters": list(value.media_posters),
    }
    if value.translation_zh is not None:
        mapping["text_zh"] = value.text_zh
        mapping["translation_zh"] = _translation_to_mapping(value.translation_zh)
    return mapping


def _quoted_from_value(value: object) -> QuotedPost | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("quoted_tweet must be a mapping or null")
    try:
        text = _mapping_string(value, "text", "quoted_tweet")
        translation_zh = _translation_from_value(value.get("translation_zh"))
        text_zh = _validate_translation_pair(
            text,
            value.get("text_zh", ""),
            translation_zh,
            "quoted_tweet.translation_zh",
        )
        return QuotedPost(
            id=_mapping_string(value, "id", "quoted_tweet"),
            author=_mapping_string(value, "author", "quoted_tweet"),
            text=text,
            created_at=_mapping_string(value, "created_at", "quoted_tweet"),
            url=_mapping_string(value, "url", "quoted_tweet"),
            media_urls=_strings(value.get("media_urls", [])),
            media_posters=_strings(value.get("media_posters", [])),
            text_zh=text_zh,
            translation_zh=translation_zh,
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


def _translation_to_mapping(
    value: TranslationMetadata | None,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, TranslationMetadata):
        raise TypeError("translation metadata must be TranslationMetadata or null")
    mapping = {
        "language": value.language,
        "model_id": value.model_id,
        "revision": value.revision,
        "source_sha256": value.source_sha256,
        "translated_at": value.translated_at,
    }
    _validate_translation_mapping(mapping)
    return mapping


def _translation_from_value(value: object) -> TranslationMetadata | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("translation metadata must be a mapping or null")
    _validate_translation_mapping(value)
    return TranslationMetadata(
        language=value["language"],
        model_id=value["model_id"],
        revision=value["revision"],
        source_sha256=value["source_sha256"],
        translated_at=value["translated_at"],
    )


def _validate_translation_mapping(value: dict[object, object]) -> None:
    if set(value) != _TRANSLATION_FIELDS:
        raise ValueError("translation metadata must contain exactly the required fields")
    for field in ("language", "model_id", "revision", "translated_at"):
        item = value[field]
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"translation metadata {field} must be a nonblank string")
    if value["language"] != "zh-CN":
        raise ValueError("translation metadata language must be zh-CN")
    source_sha256 = value["source_sha256"]
    if not isinstance(source_sha256, str) or not _SHA256.fullmatch(source_sha256):
        raise ValueError("translation metadata source_sha256 must be 64 lowercase hex characters")


def _validate_translation_pair(
    source_text: object,
    text_zh: object,
    metadata: TranslationMetadata | None,
    field: str,
) -> str:
    if not isinstance(text_zh, str):
        raise TypeError(f"{field} text_zh must be a string")
    normalized_text_zh = text_zh.strip("\n")
    mapping = _translation_to_mapping(metadata)
    has_text = bool(normalized_text_zh.strip())
    if has_text != (mapping is not None):
        raise ValueError(f"{field}: text_zh and translation_zh must both be present or absent")
    if mapping is not None:
        if not isinstance(source_text, str):
            raise TypeError(f"{field} source text must be a string")
        expected = hashlib.sha256(source_text.strip().encode("utf-8")).hexdigest()
        if mapping["source_sha256"] != expected:
            raise ValueError(f"{field}.source_sha256 does not match source text")
    return normalized_text_zh if has_text else ""


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
