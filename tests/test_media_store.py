from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from xrag.media_store import MediaStore, _AllowlistedRedirectHandler
from xrag.models import LocalMedia, Post, QuotedPost


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        content_type: str,
        url: str,
        *,
        content_length: int | None = None,
    ) -> None:
        self._stream = BytesIO(payload)
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def make_post(**changes: object) -> Post:
    values: dict[str, object] = {
        "id": "123",
        "author": "main",
        "text": "body",
        "created_at": "",
        "url": "https://x.com/i/status/123",
    }
    values.update(changes)
    return Post(**values)  # type: ignore[arg-type]


def test_archive_downloads_image_and_quoted_video_poster(tmp_path: Path) -> None:
    payloads = {
        "https://pbs.twimg.com/media/image": (b"jpeg-image", "image/jpeg"),
        "https://pbs.twimg.com/media/poster": (b"png-poster", "image/png"),
    }

    def open_url(request: Request, timeout: float) -> FakeResponse:
        assert timeout == 20.0
        payload, content_type = payloads[request.full_url]
        return FakeResponse(
            payload,
            content_type,
            request.full_url,
            content_length=len(payload),
        )

    post = make_post(
        media_urls=("https://pbs.twimg.com/media/image",),
        quoted_post=QuotedPost(
            "456",
            "quoted",
            "quote",
            "",
            "https://x.com/i/status/456",
            media_posters=("https://pbs.twimg.com/media/poster",),
        ),
    )
    result = MediaStore(tmp_path / "data/media", open_url=open_url).archive(post)

    assert result.failures == ()
    assert [item.relative_path for item in result.post.local_media] == [
        "../media/123/image-01.jpg",
        "../media/123/quoted-video-poster-01.png",
    ]
    assert (tmp_path / "data/media/123/image-01.jpg").read_bytes() == b"jpeg-image"
    assert (tmp_path / "data/media/123/quoted-video-poster-01.png").read_bytes() == b"png-poster"


@pytest.mark.parametrize(
    "source_url",
    [
        "http://pbs.twimg.com/media/insecure",
        "https://example.com/media/not-x",
    ],
)
def test_archive_rejects_untrusted_source_before_opening(
    tmp_path: Path, source_url: str
) -> None:
    calls: list[str] = []

    def open_url(request: Request, timeout: float) -> FakeResponse:
        calls.append(request.full_url)
        raise AssertionError("untrusted URL must not be opened")

    result = MediaStore(tmp_path / "media", open_url=open_url).archive(
        make_post(media_urls=(source_url,))
    )

    assert len(result.failures) == 1
    assert calls == []
    assert not list(tmp_path.rglob("*.tmp"))


def test_redirect_handler_rejects_untrusted_target_before_following() -> None:
    handler = _AllowlistedRedirectHandler()

    with pytest.raises(ValueError, match="allowlisted"):
        handler.redirect_request(
            Request("https://pbs.twimg.com/media/source"),
            None,
            302,
            "Found",
            {},
            "https://example.com/redirected",
        )


def test_archive_rejects_untrusted_final_redirect_url(tmp_path: Path) -> None:
    def open_url(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(b"image", "image/jpeg", "https://example.com/final")

    result = MediaStore(tmp_path / "media", open_url=open_url).archive(
        make_post(media_urls=("https://pbs.twimg.com/media/source",))
    )

    assert len(result.failures) == 1
    assert result.post.local_media == ()
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize(
    ("payload", "content_type", "content_length", "max_bytes"),
    [
        (b"text", "text/plain", 4, 100),
        (b"large", "image/jpeg", 5, 4),
        (b"stream-large", "image/jpeg", None, 4),
    ],
)
def test_archive_rejects_invalid_type_or_oversized_file_without_residue(
    tmp_path: Path,
    payload: bytes,
    content_type: str,
    content_length: int | None,
    max_bytes: int,
) -> None:
    def open_url(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(
            payload,
            content_type,
            request.full_url,
            content_length=content_length,
        )

    result = MediaStore(
        tmp_path / "media",
        open_url=open_url,
        max_bytes=max_bytes,
    ).archive(make_post(media_urls=("https://pbs.twimg.com/media/source",)))

    assert len(result.failures) == 1
    assert result.post.local_media == ()
    assert not list(tmp_path.rglob("*.tmp"))
    assert not list((tmp_path / "media").rglob("*.jpg"))


def test_archive_never_downloads_video_body(tmp_path: Path) -> None:
    calls: list[str] = []

    def open_url(request: Request, timeout: float) -> FakeResponse:
        calls.append(request.full_url)
        raise AssertionError("video body must not be requested")

    result = MediaStore(tmp_path / "media", open_url=open_url).archive(
        make_post(media_urls=("https://video.twimg.com/ext_tw_video/file.mp4",))
    )

    assert result.post.local_media == ()
    assert result.failures == ()
    assert calls == []


def test_archive_reuses_exact_existing_source_mapping(tmp_path: Path) -> None:
    directory = tmp_path / "data/media"

    def first_open(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(b"image", "image/jpeg", request.full_url)

    first = MediaStore(directory, open_url=first_open).archive(
        make_post(media_urls=("https://pbs.twimg.com/media/source",))
    )

    def fail_if_opened(request: Request, timeout: float) -> FakeResponse:
        raise AssertionError("matching existing source must be reused")

    second = MediaStore(directory, open_url=fail_if_opened).archive(first.post)

    assert second.failures == ()
    assert second.post.local_media == first.post.local_media


def test_archive_does_not_reuse_ordinal_file_for_different_source(tmp_path: Path) -> None:
    directory = tmp_path / "data/media"
    target = directory / "123/image-01.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    post = make_post(
        media_urls=("https://pbs.twimg.com/media/new",),
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/old",
                "../media/123/image-01.jpg",
                "image/jpeg",
            ),
        ),
    )
    calls: list[str] = []

    def open_url(request: Request, timeout: float) -> FakeResponse:
        calls.append(request.full_url)
        return FakeResponse(b"new", "image/jpeg", request.full_url)

    result = MediaStore(directory, open_url=open_url).archive(post)

    assert calls == ["https://pbs.twimg.com/media/new"]
    assert result.post.local_media[0].source_url.endswith("/new")
    assert target.read_bytes() == b"new"


def test_failed_replacement_preserves_existing_file(tmp_path: Path) -> None:
    directory = tmp_path / "data/media"
    target = directory / "123/image-01.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")

    def open_url(request: Request, timeout: float) -> FakeResponse:
        return FakeResponse(b"invalid", "text/plain", request.full_url)

    result = MediaStore(directory, open_url=open_url).archive(
        make_post(media_urls=("https://pbs.twimg.com/media/new",))
    )

    assert len(result.failures) == 1
    assert target.read_bytes() == b"existing"
    assert not list(tmp_path.rglob("*.tmp"))
