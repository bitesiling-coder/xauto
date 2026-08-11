from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TranslationMetadata:
    language: str
    model_id: str
    revision: str
    source_sha256: str
    translated_at: str


@dataclass(frozen=True)
class QuotedPost:
    id: str
    author: str
    text: str
    created_at: str
    url: str
    media_urls: tuple[str, ...] = ()
    media_posters: tuple[str, ...] = ()
    text_zh: str = ""
    translation_zh: TranslationMetadata | None = None


@dataclass(frozen=True)
class LocalMedia:
    owner: Literal["post", "quoted"]
    kind: Literal["image", "video_poster"]
    source_url: str
    relative_path: str
    content_type: str


@dataclass(frozen=True)
class Post:
    id: str
    author: str
    text: str
    created_at: str
    url: str
    bio: str = ""
    likes: int = 0
    views: int = 0
    media_urls: tuple[str, ...] = ()
    media_posters: tuple[str, ...] = ()
    quoted_post: QuotedPost | None = None
    local_media: tuple[LocalMedia, ...] = ()
    source_keywords: tuple[str, ...] = ()
    source_type: str = "opencli"
    text_zh: str = ""
    translation_zh: TranslationMetadata | None = None

    @property
    def searchable_text(self) -> str:
        parts = [self.text.strip(), self.text_zh.strip()]
        if self.quoted_post is not None:
            parts.extend(
                [self.quoted_post.text.strip(), self.quoted_post.text_zh.strip()]
            )
        return "\n\n".join(part for part in parts if part)


@dataclass(frozen=True)
class SearchHit:
    post_id: str
    text: str
    author: str
    created_at: str
    url: str
    score: float
    markdown_path: str
