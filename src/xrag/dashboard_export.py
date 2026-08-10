from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .config import AppConfig
from .dashboard_scoring import TOPICS, RankedPost, Topic, rank_posts
from .markdown_store import MarkdownStore


_UNSAFE_PUBLIC_CONTENT = re.compile(
    r"auth[\s_.-]*token|ct0|authorization|(?<![a-z])[a-z]:(?:\\+|/(?!/))|/mnt/[a-z]/|/home/",
    re.IGNORECASE,
)


def assert_public_content(content: str) -> None:
    """Reject credentials and machine-local absolute paths from public output."""
    if _UNSAFE_PUBLIC_CONTENT.search(content):
        raise ValueError("unsafe public output")


class DashboardBuilder:
    def __init__(
        self,
        config: AppConfig,
        markdown: MarkdownStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.markdown = markdown
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def build(self) -> dict[str, object]:
        now = self._clock()
        ranked = rank_posts(
            (post for _, post in self.markdown.iter_posts()),
            now=now,
            timezone_name=self.config.timezone,
            configured_keywords=self.config.keywords,
        )
        if not ranked:
            raise ValueError("No valid dashboard candidates")
        for item in ranked:
            _validate_x_url(item.post.url)
        _validate_static_sources(self.config.dashboard_source_dir)
        local_now = now.astimezone(ZoneInfo(self.config.timezone))
        prepared_media = [self._prepare_media(item) for item in ranked]
        public_posts = [
            _public_post(item, media_entries)
            for item, (media_entries, _) in zip(ranked, prepared_media, strict=True)
        ]
        media_count = sum(len(entries) for entries, _ in prepared_media)
        payload = {
            "version": 1,
            "generated_at": local_now.isoformat(),
            "timezone": self.config.timezone,
            "fallback_used": any(item.fallback for item in ranked),
            "summary": {
                "posts": len(ranked),
                "authors": len(
                    {
                        item.post.author.strip().casefold()
                        for item in ranked
                        if item.post.author.strip()
                    }
                ),
                "media": media_count,
                "engagement": sum(
                    max(0, item.post.likes) + max(0, item.post.views) for item in ranked
                ),
            },
            "topics": [_public_topic(topic, ranked) for topic in TOPICS],
            "posts": public_posts,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        assert_public_content(content)

        output = self.config.dashboard_dir
        data_dir = output / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        _copy_static(self.config.dashboard_source_dir, output)
        _write_atomic(output / ".nojekyll", b"")
        for _, media_files in prepared_media:
            for destination, media_bytes in media_files:
                if not destination.exists():
                    _write_atomic(destination, media_bytes)
        dated = data_dir / f"{local_now.strftime('%Y-%m-%dT%H%M%S%z')}.json"
        encoded = content.encode("utf-8")
        _write_atomic(dated, encoded)
        latest = data_dir / "latest.json"
        _write_atomic(latest, encoded)
        return {
            "output_path": latest,
            "dated_snapshot_path": dated,
            "post_count": len(ranked),
            "media_count": media_count,
        }

    def _prepare_media(
        self, item: RankedPost
    ) -> tuple[list[dict[str, str]], list[tuple[Path, bytes]]]:
        entries: list[dict[str, str]] = []
        files: list[tuple[Path, bytes]] = []
        media_root = self.config.media_dir.resolve()
        for local in item.post.local_media:
            try:
                source = (self.config.markdown_dir / local.relative_path).resolve()
            except (OSError, RuntimeError) as error:
                raise ValueError("unsafe media path") from error
            if source == media_root or media_root not in source.parents:
                raise ValueError("unsafe media path")
            if not source.is_file():
                continue
            try:
                content = source.read_bytes()
            except OSError:
                continue
            suffix = source.suffix.lower()
            if not _valid_media(content, suffix):
                continue
            digest = hashlib.sha256(content).hexdigest()
            relative_url = f"assets/media/{digest}{suffix}"
            entries.append(
                {
                    "url": relative_url,
                    "type": local.kind,
                    "alt": item.post.text.strip()[:160]
                    or f"@{item.post.author.strip()} 的配图",
                }
            )
            files.append(
                (
                    self.config.dashboard_dir.joinpath(*relative_url.split("/")),
                    content,
                )
            )
        return entries, files


def _public_post(
    item: RankedPost, media: list[dict[str, str]]
) -> dict[str, object]:
    post = item.post
    return {
        "id": post.id,
        "author": post.author,
        "text": post.text,
        "created_at": post.created_at,
        "url": post.url,
        "likes": post.likes,
        "views": post.views,
        "topic": item.topic.id,
        "family": item.topic.family,
        "keywords": list(post.source_keywords),
        "score": round(item.score, 6),
        "fallback": item.fallback,
        "media": media,
    }


def _valid_media(content: bytes, suffix: str) -> bool:
    if suffix in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def _public_topic(topic: Topic, ranked: list[RankedPost]) -> dict[str, object]:
    visible = [item for item in ranked if item.topic == topic]
    configured_queries = [
        keyword for item in visible for keyword in item.post.source_keywords
    ]
    top_query = Counter(configured_queries).most_common(1)
    return {
        "id": topic.id,
        "label": topic.label,
        "family": topic.family,
        "posts": len(visible),
        "score": round(
            sum(item.score for item in visible) / len(visible), 6
        )
        if visible
        else 0,
        "top_keyword": _short_keyword(top_query[0][0]) if top_query else "",
    }


def _short_keyword(query: str) -> str:
    quoted = re.search(r'"([^"\r\n]+)"', query)
    if quoted:
        value = quoted.group(1)
    else:
        value = re.split(r"\s+OR\s+", query, maxsplit=1, flags=re.IGNORECASE)[0]
    return value.strip()[:48]


def _validate_x_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("invalid X URL") from error
    if (
        not isinstance(url, str)
        or url != url.strip()
        or parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or hostname is None
        or hostname.casefold()
        not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
    ):
        raise ValueError("invalid X URL")


def _validate_static_sources(source: Path) -> None:
    if not all(
        source.joinpath(relative).is_file()
        for relative in ("index.html", "assets/styles.css", "assets/app.js")
    ):
        raise ValueError("static source is incomplete")


def _copy_static(source: Path, output: Path) -> None:
    for relative in (Path("index.html"), Path("assets/styles.css"), Path("assets/app.js")):
        source_path = source / relative
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(destination, source_path.read_bytes())


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".xrag-",
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
