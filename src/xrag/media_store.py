from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import LocalMedia, Post


_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_ALLOWED_HOSTS = frozenset({"pbs.twimg.com"})
_VIDEO_HOSTS = frozenset({"video.twimg.com"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_CHUNK_SIZE = 64 * 1024


class MediaValidationError(ValueError):
    """A safe-to-log media validation failure."""


class _AllowlistedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class MediaFailure:
    owner: str
    kind: str
    safe_source: str
    error_name: str
    reason: str


@dataclass(frozen=True)
class MediaArchiveResult:
    post: Post
    failures: tuple[MediaFailure, ...]


@dataclass(frozen=True)
class _Source:
    owner: str
    kind: str
    url: str
    ordinal: int

    @property
    def stem(self) -> str:
        prefixes = {
            ("post", "image"): "image",
            ("post", "video_poster"): "video-poster",
            ("quoted", "image"): "quoted-image",
            ("quoted", "video_poster"): "quoted-video-poster",
        }
        return f"{prefixes[(self.owner, self.kind)]}-{self.ordinal:02d}"


class MediaStore:
    def __init__(
        self,
        directory: Path,
        *,
        open_url: Callable[[Request, float], Any] | None = None,
        timeout: float = 20.0,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        self.directory = Path(directory)
        if open_url is None:
            opener = build_opener(_AllowlistedRedirectHandler())
            open_url = lambda request, timeout: opener.open(request, timeout=timeout)
        self._open_url = open_url
        self.timeout = timeout
        self.max_bytes = max_bytes

    def archive(self, post: Post) -> MediaArchiveResult:
        saved: list[LocalMedia] = []
        failures: list[MediaFailure] = []
        for source in _sources(post):
            if _hostname(source.url) in _VIDEO_HOSTS and source.kind == "image":
                continue
            try:
                _validate_post_id(post.id)
                _validate_source_url(source.url)
                existing = self._existing_media(post, source)
                if existing is not None:
                    saved.append(existing)
                    continue
                saved.append(self._download(post.id, source))
            except Exception as error:
                failures.append(
                    MediaFailure(
                        owner=source.owner,
                        kind=source.kind,
                        safe_source=_safe_source(source.url),
                        error_name=type(error).__name__,
                        reason=_safe_reason(error),
                    )
                )
        return MediaArchiveResult(
            replace(post, local_media=tuple(saved)),
            tuple(failures),
        )

    def _existing_media(self, post: Post, source: _Source) -> LocalMedia | None:
        post_directory = (self.directory / post.id).resolve()
        for item in post.local_media:
            if (
                item.owner != source.owner
                or item.kind != source.kind
                or item.source_url != source.url
            ):
                continue
            extension = _CONTENT_TYPES.get(item.content_type.lower())
            if extension is None:
                continue
            expected_relative = f"../media/{post.id}/{source.stem}{extension}"
            if item.relative_path != expected_relative:
                continue
            candidate = (self.directory.parent / "markdown" / item.relative_path).resolve()
            if candidate.parent != post_directory or not candidate.is_file():
                continue
            return item
        return None

    def _download(self, post_id: str, source: _Source) -> LocalMedia:
        request = Request(source.url, headers={"User-Agent": "xrag/0.1"})
        temporary_path: Path | None = None
        try:
            with self._open_url(request, self.timeout) as response:
                _validate_source_url(str(response.geturl()))
                content_type = _content_type(response.headers)
                extension = _CONTENT_TYPES.get(content_type)
                if extension is None:
                    raise MediaValidationError("unsupported image content type")
                content_length = _content_length(response.headers)
                if content_length is not None and content_length > self.max_bytes:
                    raise MediaValidationError("media file exceeds size limit")

                post_directory = self.directory / post_id
                post_directory.mkdir(parents=True, exist_ok=True)
                target = post_directory / f"{source.stem}{extension}"
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=post_directory,
                    prefix=f".{source.stem}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    written = 0
                    while True:
                        chunk = response.read(_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise MediaValidationError("media file exceeds size limit")
                        temporary_file.write(chunk)
                    temporary_file.flush()
                    os.fsync(temporary_file.fileno())
                os.replace(temporary_path, target)
                temporary_path = None
                return LocalMedia(
                    owner=source.owner,  # type: ignore[arg-type]
                    kind=source.kind,  # type: ignore[arg-type]
                    source_url=source.url,
                    relative_path=f"../media/{post_id}/{target.name}",
                    content_type=content_type,
                )
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _sources(post: Post) -> Iterable[_Source]:
    groups: list[tuple[str, str, tuple[str, ...]]] = [
        ("post", "image", post.media_urls),
        ("post", "video_poster", post.media_posters),
    ]
    if post.quoted_post is not None:
        groups.extend(
            [
                ("quoted", "image", post.quoted_post.media_urls),
                ("quoted", "video_poster", post.quoted_post.media_posters),
            ]
        )
    for owner, kind, urls in groups:
        for ordinal, url in enumerate(urls, start=1):
            yield _Source(owner, kind, url, ordinal)


def _validate_post_id(post_id: str) -> None:
    if not isinstance(post_id, str) or not _SAFE_ID.fullmatch(post_id):
        raise MediaValidationError("unsafe post ID")


def _validate_source_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise MediaValidationError("invalid media URL") from error
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in _ALLOWED_HOSTS
        or port not in (None, 443)
    ):
        raise MediaValidationError("media URL is not HTTPS on an allowlisted host")


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _safe_source(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "invalid"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "invalid-media-url"


def _safe_reason(error: Exception) -> str:
    if isinstance(error, MediaValidationError):
        return str(error)
    if isinstance(error, OSError):
        return "media storage failed"
    return "media download failed"


def _content_type(headers: Any) -> str:
    value = headers.get("Content-Type", "")
    return str(value).split(";", 1)[0].strip().lower()


def _content_length(headers: Any) -> int | None:
    value = headers.get("Content-Length")
    if value in (None, ""):
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as error:
        raise MediaValidationError("invalid content length") from error
    if length < 0:
        raise MediaValidationError("invalid content length")
    return length
