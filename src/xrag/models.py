from __future__ import annotations

from dataclasses import dataclass


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
    source_keywords: tuple[str, ...] = ()
    source_type: str = "opencli"


@dataclass(frozen=True)
class SearchHit:
    post_id: str
    text: str
    author: str
    created_at: str
    url: str
    score: float
    markdown_path: str
