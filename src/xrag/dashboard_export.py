from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .config import AppConfig
from .dashboard_scoring import TOPICS, RankedPost, Topic, rank_posts
from .locking import writer_lock
from .markdown_store import MarkdownStore


_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?<![a-z0-9_])[\"']?(?:openai_api_key|aws_secret_access_key|"
    r"auth[\s_.-]*token|access[\s_.-]*token|refresh[\s_.-]*token|"
    r"client[\s_.-]*secret|password|passwd|cookie|authorization|ct0)"
    r"[\"']?(?![a-z0-9_])\s*[:=]",
    re.IGNORECASE,
)
_PRIVATE_PATH = re.compile(
    r"\\{2,}|(?<![a-z])[a-z]:(?:\\+|/(?!/))|/mnt/[a-z]/|"
    r"/(?:home|root|users)/",
    re.IGNORECASE,
)


def assert_public_content(content: str) -> None:
    """Reject credentials and machine-local absolute paths from public output."""
    if _CREDENTIAL_ASSIGNMENT.search(content) or _PRIVATE_PATH.search(content):
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
        with writer_lock(self.config.root):
            return self._build_locked()

    def _build_locked(self) -> dict[str, object]:
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
        prepared_static = _prepare_static_sources(
            self.config.root, self.config.dashboard_source_dir
        )
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
        guard = _OutputGuard(self.config.root, output)
        data_dir = output / "data"
        guard.ensure_directory(data_dir)
        guard.ensure_directory(output / "assets")
        if media_count:
            guard.ensure_directory(output / "assets" / "media")
        for _, media_files in prepared_media:
            for destination, media_bytes in media_files:
                _validate_existing_media_asset(destination, media_bytes)
        encoded = content.encode("utf-8")
        base_dated = data_dir / f"{local_now.strftime('%Y-%m-%dT%H%M%S%z')}.json"
        dated, write_dated = _select_versioned_snapshot(base_dated, encoded)
        guard.validate_target(dated)
        _copy_static(prepared_static, output, guard)
        _write_atomic(output / ".nojekyll", b"", guard)
        for _, media_files in prepared_media:
            for destination, media_bytes in media_files:
                if not destination.exists():
                    _write_atomic(destination, media_bytes, guard)
        if write_dated:
            _write_atomic(dated, encoded, guard)
        latest = data_dir / "latest.json"
        _write_atomic(latest, encoded, guard)
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
        seen_digests: set[str] = set()
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
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
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


def _validate_existing_media_asset(destination: Path, expected: bytes) -> None:
    if not destination.exists() and not _is_link_or_junction(destination):
        return
    if _is_link_or_junction(destination) or not destination.is_file():
        raise ValueError("invalid existing media asset")
    try:
        actual = destination.read_bytes()
    except OSError as error:
        raise ValueError("invalid existing media asset") from error
    expected_digest = hashlib.sha256(expected).hexdigest()
    if (
        not _valid_media(actual, destination.suffix.lower())
        or hashlib.sha256(actual).hexdigest() != expected_digest
        or destination.stem != expected_digest
    ):
        raise ValueError("invalid existing media asset")


def _select_versioned_snapshot(base: Path, content: bytes) -> tuple[Path, bool]:
    existing = _read_existing_snapshot(base)
    if existing is None:
        return base, True
    if existing == content:
        return base, False
    digest = hashlib.sha256(content).hexdigest()[:12]
    suffixed = base.with_name(f"{base.stem}-{digest}{base.suffix}")
    existing_suffixed = _read_existing_snapshot(suffixed)
    if existing_suffixed is None:
        return suffixed, True
    if existing_suffixed == content:
        return suffixed, False
    raise ValueError("conflicting versioned snapshot")


def _read_existing_snapshot(path: Path) -> bytes | None:
    if not path.exists() and not _is_link_or_junction(path):
        return None
    if _is_link_or_junction(path) or not path.is_file():
        raise ValueError("conflicting versioned snapshot")
    try:
        return path.read_bytes()
    except OSError as error:
        raise ValueError("conflicting versioned snapshot") from error


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


def _prepare_static_sources(
    project_root: Path, source: Path
) -> list[tuple[Path, bytes]]:
    trusted_project = project_root.resolve()
    source = Path(source)
    if source.parent.resolve() != trusted_project or _is_link_or_junction(source):
        raise ValueError("static source is incomplete")
    try:
        trusted_source = source.resolve(strict=True)
    except OSError as error:
        raise ValueError("static source is incomplete") from error
    if trusted_source.parent != trusted_project:
        raise ValueError("static source is incomplete")

    prepared: list[tuple[Path, bytes]] = []
    for relative in (Path("index.html"), Path("assets/styles.css"), Path("assets/app.js")):
        source_path = source / relative
        if any(_is_link_or_junction(path) for path in _paths_from(source, source_path)):
            raise ValueError("static source is incomplete")
        try:
            resolved = source_path.resolve(strict=True)
        except OSError as error:
            raise ValueError("static source is incomplete") from error
        if resolved == trusted_source or trusted_source not in resolved.parents:
            raise ValueError("static source is incomplete")
        if not resolved.is_file():
            raise ValueError("static source is incomplete")
        try:
            content = resolved.read_bytes()
            decoded = content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ValueError("static source is incomplete") from error
        assert_public_content(decoded)
        prepared.append((relative, content))
    return prepared


def _copy_static(
    prepared: list[tuple[Path, bytes]], output: Path, guard: _OutputGuard
) -> None:
    for relative, content in prepared:
        destination = output / relative
        _write_atomic(destination, content, guard)


def _paths_from(root: Path, target: Path) -> list[Path]:
    relative = target.relative_to(root)
    paths: list[Path] = []
    current = root
    for part in relative.parts:
        current = current / part
        paths.append(current)
    return paths


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


class _OutputGuard:
    def __init__(self, project_root: Path, output_root: Path) -> None:
        self.project = Path(project_root).absolute()
        self.output = Path(output_root).absolute()
        try:
            self.output_relative = self.output.relative_to(self.project)
            self.trusted_project = self.project.resolve(strict=True)
        except (OSError, ValueError) as error:
            raise ValueError("unsafe dashboard output") from error
        if not self.output_relative.parts:
            raise ValueError("unsafe dashboard output")
        self.trusted_output = self.trusted_project / self.output_relative
        self._validate_project_chain()

    def _validate_project_chain(self) -> None:
        current = self.project
        for part in self.output_relative.parts:
            current = current / part
            if _is_link_or_junction(current):
                raise ValueError("unsafe dashboard output")
            if current.exists():
                resolved = current.resolve()
                if (
                    resolved == self.trusted_project
                    or self.trusted_project not in resolved.parents
                ):
                    raise ValueError("unsafe dashboard output")

    def ensure_directory(self, directory: Path) -> None:
        directory = Path(directory).absolute()
        try:
            relative = directory.relative_to(self.output)
        except ValueError as error:
            raise ValueError("unsafe dashboard output") from error
        self._validate_project_chain()
        current = self.output
        for part in relative.parts:
            self._ensure_one_directory(current)
            current = current / part
        self._ensure_one_directory(current)

    def _ensure_one_directory(self, directory: Path) -> None:
        if _is_link_or_junction(directory):
            raise ValueError("unsafe dashboard output")
        if directory.exists():
            if not directory.is_dir():
                raise ValueError("unsafe dashboard output")
        else:
            try:
                directory.mkdir()
            except OSError as error:
                raise ValueError("unsafe dashboard output") from error
        if _is_link_or_junction(directory):
            raise ValueError("unsafe dashboard output")
        try:
            relative = directory.relative_to(self.output)
            expected = self.trusted_output / relative
        except ValueError as error:
            raise ValueError("unsafe dashboard output") from error
        if directory.resolve() != expected:
            raise ValueError("unsafe dashboard output")

    def validate_target(self, path: Path) -> None:
        path = Path(path).absolute()
        try:
            relative = path.relative_to(self.output)
        except ValueError as error:
            raise ValueError("unsafe dashboard output") from error
        if not relative.parts:
            raise ValueError("unsafe dashboard output")
        self.ensure_directory(path.parent)
        if _is_link_or_junction(path) or (path.exists() and not path.is_file()):
            raise ValueError("unsafe dashboard output")
        if path.parent.resolve() != (self.trusted_output / relative).parent:
            raise ValueError("unsafe dashboard output")


def _write_atomic(path: Path, content: bytes, guard: _OutputGuard) -> None:
    guard.validate_target(path)
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
        guard.validate_target(path)
        os.replace(temporary_path, path)
        temporary_path = None
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
