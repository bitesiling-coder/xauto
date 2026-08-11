from __future__ import annotations

import sys
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import AppConfig
import xrag.dashboard_export as dashboard_export
from xrag.dashboard_export import DashboardBuilder, _public_post, assert_public_content
from xrag.dashboard_scoring import TOPICS, RankedPost
from xrag.markdown_store import MarkdownStore
from xrag.models import LocalMedia, Post, TranslationMetadata


def test_publisher_import_needs_only_the_standard_library() -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import xrag.dashboard_publish; print('publisher-imported')",
        ],
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "publisher-imported\n"


QUERIES = (
    '"Autonomous AI Agents" OR "Agent Security"',
    '"World Models" OR Embodied AI',
    'RWA OR "Stablecoin Payments"',
    '"Prediction Markets" OR MiCA',
)
NOW = datetime(2026, 8, 10, 12, 34, 56, tzinfo=ZoneInfo("UTC"))


def config(root: Path) -> AppConfig:
    return AppConfig(root, False, "03:00", "Asia/Singapore", 7, 0, QUERIES, "model")


def prepare_static(root: Path) -> None:
    source = root / "dashboard"
    source.joinpath("assets").mkdir(parents=True)
    source.joinpath("index.html").write_text("<!doctype html>仪表盘", encoding="utf-8")
    source.joinpath("assets", "styles.css").write_text("body{}", encoding="utf-8")
    source.joinpath("assets", "app.js").write_text("void 0;", encoding="utf-8")


def save_post(root: Path, **overrides: object) -> Path:
    values: dict[str, object] = {
        "id": "post-1",
        "author": "艾达",
        "text": "智能体安全动态",
        "created_at": NOW.isoformat(),
        "url": "https://x.com/ada/status/post-1",
        "likes": 3,
        "views": 17,
        "source_keywords": (QUERIES[0],),
    }
    values.update(overrides)
    return MarkdownStore(config(root).markdown_dir).upsert(
        Post(**values)  # type: ignore[arg-type]
    )


def translation_for(text: str) -> TranslationMetadata:
    return TranslationMetadata(
        language="zh-CN",
        model_id="translator-v1",
        revision="r1",
        source_sha256=hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
        translated_at="2026-08-10T00:00:00Z",
    )


def ranked_post(post: Post) -> RankedPost:
    return RankedPost(
        post=post,
        topic=TOPICS[0],
        score=0.75,
        engagement=0.5,
        freshness=0.5,
        topic_frequency=1.0,
        completeness=1.0,
        fallback=False,
    )


def test_public_post_exports_only_valid_nonblank_chinese_translation() -> None:
    source = "Autonomous agents need stronger security."
    translated = Post(
        "post-1",
        "Ada",
        source,
        NOW.isoformat(),
        "https://x.com/ada/status/post-1",
        text_zh="\u4e2d\u6587\u8bd1\u6587",
        translation_zh=translation_for(source),
    )
    missing_metadata = Post(
        "post-2",
        "Ada",
        source,
        NOW.isoformat(),
        "https://x.com/ada/status/post-2",
        text_zh="\u7f3a\u5c11\u7ffb\u8bd1\u5143\u6570\u636e\u3002",
    )
    blank_translation = Post(
        "post-3",
        "Ada",
        source,
        NOW.isoformat(),
        "https://x.com/ada/status/post-3",
        text_zh="   ",
        translation_zh=translation_for(source),
    )

    assert _public_post(ranked_post(translated), [])["text_zh"] == "\u4e2d\u6587\u8bd1\u6587"
    assert "text_zh" not in _public_post(ranked_post(missing_metadata), [])
    assert "text_zh" not in _public_post(ranked_post(blank_translation), [])


def test_public_post_translation_is_checked_for_unsafe_public_content() -> None:
    source = "Autonomous agents need stronger security."
    unsafe_translation = r"C:\\Users\\name\\private"
    post = Post(
        "post-1",
        "Ada",
        source,
        NOW.isoformat(),
        "https://x.com/ada/status/post-1",
        text_zh=unsafe_translation,
        translation_zh=translation_for(source),
    )
    content = json.dumps({"posts": [_public_post(ranked_post(post), [])]})

    with pytest.raises(ValueError, match="unsafe public output") as error:
        assert_public_content(content)

    assert source not in str(error.value)
    assert unsafe_translation not in str(error.value)


def seed_latest(root: Path) -> tuple[Path, bytes]:
    latest = config(root).dashboard_dir / "data" / "latest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    previous = b'{"previous": true}\n'
    latest.write_bytes(previous)
    return latest, previous


def build(root: Path) -> dict[str, object]:
    settings = config(root)
    return DashboardBuilder(
        settings, MarkdownStore(settings.markdown_dir), clock=lambda: NOW
    ).build()


@contextmanager
def no_writer_lock(root: Path):
    yield


@pytest.mark.parametrize(
    "content",
    [
        '"auth_token": "secret"',
        '"AUTH-TOKEN": "secret"',
        '"auth.token": "secret"',
        '"auth token": "secret"',
        '"ct0": "secret"',
        '"Authorization": "Bearer secret"',
        r'"path": "C:\\Users\\name\\archive"',
        r'"path": "z:/private/archive"',
        '"path": "/mnt/c/private/archive"',
        '"path": "/home/name/archive"',
    ],
)
def test_assert_public_content_rejects_credentials_and_absolute_paths(content: str) -> None:
    with pytest.raises(ValueError, match="unsafe public output"):
        assert_public_content(content)


def test_assert_public_content_accepts_safe_relative_public_content() -> None:
    assert_public_content('{"url": "assets/media/image.jpg", "text": "安全"}')


@pytest.mark.parametrize(
    "content",
    [
        "React0 component",
        "reauthorization flow",
        "React0=value",
        "reauthorization=value",
        "TOKENIZED=value",
    ],
)
def test_assert_public_content_does_not_match_credential_substrings(
    content: str,
) -> None:
    assert_public_content(content)


@pytest.mark.parametrize(
    "content",
    [
        r"regex uses \d+ for digits",
        "https://example.com/users/alice/profile",
        json.dumps({"text": r"regex uses \d+ for digits"}),
        json.dumps({"url": "https://example.com/users/alice/profile"}),
    ],
)
def test_assert_public_content_accepts_regex_and_https_users_path(content: str) -> None:
    assert_public_content(content)


@pytest.mark.parametrize(
    "content",
    [
        "OPENAI_API_KEY=secret",
        "ANTHROPIC_API_KEY=secret",
        "GITHUB_TOKEN=secret",
        "AWS_SESSION_TOKEN=secret",
        "AWS_ACCESS_KEY_ID=secret",
        "AZURE_ACCESS_KEY_ID=secret",
        "SESSION_CREDENTIAL=secret",
        "SERVICE_SECRET=secret",
        "DATABASE_PASSWORD=secret",
        "DATABASE_PASSWD=secret",
        "SIGNING_PRIVATE_KEY=secret",
        "AWS_SECRET_ACCESS_KEY: secret",
        "access_token=secret",
        "refresh-token: secret",
        '"client_secret": "secret"',
        "password=secret",
        "passwd: secret",
        "Cookie: session=secret",
        '"cookie"="secret"',
        r"\\server\share\archive",
        "/root/private/archive",
        "/Users/person/archive",
    ],
)
def test_assert_public_content_rejects_common_credentials_and_path_families(
    content: str,
) -> None:
    with pytest.raises(ValueError, match="unsafe public output"):
        assert_public_content(content)


@pytest.mark.parametrize(
    "payload",
    [
        {"ANTHROPIC_API_KEY": "secret"},
        {"nested": {"GITHUB_TOKEN": "secret"}},
        {"items": [{"AWS_ACCESS_KEY_ID": "secret"}]},
    ],
)
def test_assert_public_content_rejects_sensitive_json_keys(payload: object) -> None:
    with pytest.raises(ValueError, match="unsafe public output"):
        assert_public_content(json.dumps(payload))


def test_build_writes_exact_public_schema_topics_static_files_and_utf8(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    settings = config(tmp_path)

    result = DashboardBuilder(
        settings,
        MarkdownStore(settings.markdown_dir),
        clock=lambda: NOW,
    ).build()

    latest = settings.dashboard_dir / "data" / "latest.json"
    dated = settings.dashboard_dir / "data" / "2026-08-10T203456+0800.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert set(payload) == {
        "version",
        "generated_at",
        "timezone",
        "fallback_used",
        "summary",
        "topics",
        "posts",
    }
    assert payload["version"] == 1
    assert payload["generated_at"] == "2026-08-10T20:34:56+08:00"
    assert payload["timezone"] == "Asia/Singapore"
    assert payload["fallback_used"] is False
    assert payload["summary"] == {
        "posts": 1,
        "authors": 1,
        "media": 0,
        "engagement": 20,
    }
    assert payload["topics"] == [
        {
            "id": topic.id,
            "label": topic.label,
            "family": topic.family,
            "posts": 1 if topic == TOPICS[0] else 0,
            "score": round(payload["posts"][0]["score"], 6)
            if topic == TOPICS[0]
            else 0,
            "top_keyword": "Autonomous AI Agents" if topic == TOPICS[0] else "",
        }
        for topic in TOPICS
    ]
    assert len(payload["posts"]) == 1
    public_post = payload["posts"][0]
    assert set(public_post) == {
        "id",
        "author",
        "text",
        "created_at",
        "url",
        "likes",
        "views",
        "topic",
        "family",
        "keywords",
        "score",
        "fallback",
        "media",
    }
    assert public_post == {
        "id": "post-1",
        "author": "艾达",
        "text": "智能体安全动态",
        "created_at": NOW.isoformat(),
        "url": "https://x.com/ada/status/post-1",
        "likes": 3,
        "views": 17,
        "topic": TOPICS[0].id,
        "family": "AI",
        "keywords": [QUERIES[0]],
        "score": public_post["score"],
        "fallback": False,
        "media": [],
    }
    assert isinstance(public_post["score"], float)
    assert dated.read_bytes() == latest.read_bytes()
    assert "艾达" in latest.read_text(encoding="utf-8")
    assert latest.read_bytes().endswith(b"\n")
    assert settings.dashboard_dir.joinpath("index.html").read_text(
        encoding="utf-8"
    ) == "<!doctype html>仪表盘"
    assert settings.dashboard_dir.joinpath("assets", "styles.css").read_text() == "body{}"
    assert settings.dashboard_dir.joinpath("assets", "app.js").read_text() == "void 0;"
    assert settings.dashboard_dir.joinpath(".nojekyll").is_file()
    assert result == {
        "output_path": latest,
        "dated_snapshot_path": dated,
        "post_count": 1,
        "media_count": 0,
    }


def test_build_exports_only_nonblank_chinese_translation(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    source = "Autonomous agents need stronger security."
    save_post(
        tmp_path,
        text=source,
        text_zh="智能体需要更强的安全保障。",
        translation_zh=translation_for(source),
    )

    build(tmp_path)

    latest = config(tmp_path).dashboard_dir / "data" / "latest.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["posts"][0]["text_zh"] == "智能体需要更强的安全保障。"


@pytest.mark.parametrize("translation", ["API_KEY=secret", r"C:\\Users\\name\\private"])
def test_build_scans_chinese_translation_for_unsafe_public_content(
    tmp_path: Path, translation: str
) -> None:
    prepare_static(tmp_path)
    source = "Autonomous agents need stronger security."
    save_post(
        tmp_path,
        text=source,
        text_zh=translation,
        translation_zh=translation_for(source),
    )

    with pytest.raises(ValueError, match="unsafe public output") as error:
        build(tmp_path)

    assert source not in str(error.value)
    assert translation not in str(error.value)


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        (
            "Sun Aug 09 13:00:00 +0000 2026",
            "2026-08-09T13:00:00+00:00",
        ),
        (
            "2026-08-10T10:30:15+05:30:15",
            "2026-08-10T05:00:00+00:00",
        ),
    ],
    ids=("x-native", "iso-second-offset"),
)
def test_build_canonicalizes_timestamp_for_frontend_validator(
    tmp_path: Path,
    created_at: str,
    expected: str,
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, created_at=created_at)

    build(tmp_path)

    latest = config(tmp_path).dashboard_dir / "data" / "latest.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["posts"][0]["created_at"] == expected

    frontend = Path(__file__).resolve().parents[1] / "dashboard" / "assets" / "app.js"
    validation = subprocess.run(
        [
            "node",
            "--input-type=module",
            "--eval",
            """
const {isValidSnapshot} = await import(process.argv[1]);
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
process.exit(isValidSnapshot(payload) ? 0 : 1);
""",
            frontend.as_uri(),
        ],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        check=False,
    )
    assert validation.returncode == 0, validation.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://x.com/ada/status/1",
        "javascript:alert(1)",
        "https://example.com/status/1",
        "https://x.com.evil.test/status/1",
        "https://user@x.com/status/1",
        "https://user:pass@twitter.com/status/1",
        "https://x.com:abc/status/1",
        "https://x.com:99999/status/1",
        "https://x.com:8443/status/1",
    ],
)
def test_invalid_or_credentialed_non_x_url_preserves_latest(
    tmp_path: Path, url: str
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, url=url)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="X URL"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_default_https_port_is_a_valid_x_url(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    url = "https://x.com:443/ada/status/1"
    save_post(tmp_path, url=url)

    output_path = build(tmp_path)["output_path"]

    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["posts"][0]["url"] == url


@pytest.mark.parametrize(
    "unsafe_text",
    [
        "AUTH_TOKEN=secret",
        "ANTHROPIC_API_KEY=secret",
        "GITHUB_TOKEN=secret",
        "AWS_SESSION_TOKEN=secret",
        "AWS_ACCESS_KEY_ID=secret",
        "ct0=secret",
        "Authorization: Bearer secret",
        r"C:\private\archive",
        "/mnt/d/private/archive",
        "/home/person/archive",
        "/root/private/archive",
        "/Users/person/archive",
        r"\\server\share\archive",
    ],
)
def test_unsafe_snapshot_content_preserves_latest(
    tmp_path: Path, unsafe_text: str
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, text=unsafe_text)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="unsafe public output"):
        build(tmp_path)

    assert latest.read_bytes() == previous


@pytest.mark.parametrize(
    "safe_text",
    [r"regex uses \d+ for digits", "https://example.com/users/alice/profile"],
)
def test_safe_regex_and_https_users_path_build_successfully(
    tmp_path: Path, safe_text: str
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, text=safe_text)

    output_path = build(tmp_path)["output_path"]

    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["posts"][0]["text"] == safe_text


def test_empty_candidates_preserve_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="No valid dashboard candidates"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_summary_does_not_count_blank_author(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, author="   ")

    output_path = build(tmp_path)["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["summary"]["authors"] == 0


def test_missing_static_source_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    (tmp_path / "dashboard" / "assets" / "app.js").unlink()
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="static source is incomplete"):
        build(tmp_path)

    assert latest.read_bytes() == previous


@pytest.mark.parametrize(
    ("relative_path", "unsafe_content"),
    [
        ("index.html", "OPENAI_API_KEY=secret"),
        ("assets/styles.css", r"/* C:\private\styles */"),
        ("assets/app.js", "const header = 'Cookie: secret';"),
    ],
)
def test_unsafe_static_source_preserves_latest(
    tmp_path: Path, relative_path: str, unsafe_content: str
) -> None:
    prepare_static(tmp_path)
    config(tmp_path).dashboard_source_dir.joinpath(*relative_path.split("/")).write_text(
        unsafe_content, encoding="utf-8"
    )
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="unsafe public output"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_malformed_utf8_static_source_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    config(tmp_path).dashboard_source_dir.joinpath("assets", "app.js").write_bytes(
        b"\xff\xfe"
    )
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="static source"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_static_source_symlink_escape_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_text("safe content", encoding="utf-8")
    source = config(tmp_path).dashboard_source_dir / "assets" / "app.js"
    source.unlink()
    try:
        os.symlink(outside, source)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="static source"):
        build(tmp_path)

    assert latest.read_bytes() == previous


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_static_source_junction_escape_preserves_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(Path, "is_junction"):
        pytest.skip("Path.is_junction is unavailable")
    prepare_static(tmp_path)
    assets = config(tmp_path).dashboard_source_dir / "assets"
    assets.joinpath("styles.css").unlink()
    assets.joinpath("app.js").unlink()
    assets.rmdir()
    external = tmp_path / "external-static"
    external.mkdir()
    external.joinpath("styles.css").write_text("body{}", encoding="utf-8")
    external.joinpath("app.js").write_text("void 0;", encoding="utf-8")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(assets), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junctions unavailable: {result.stderr or result.stdout}")
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)
    monkeypatch.setattr(dashboard_export, "writer_lock", no_writer_lock)

    with pytest.raises(ValueError, match="static source"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def _seed_output_link_attack(
    root: Path, component: str, link_factory: object
) -> tuple[Path, bytes, Path, bytes]:
    output = config(root).dashboard_dir
    external = root / f"external-{component.replace('/', '-')}"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel_bytes = b"external sentinel"
    sentinel.write_bytes(sentinel_bytes)
    previous = b'{"previous": true}\n'
    if component == "root":
        target = output
        target.parent.mkdir(parents=True, exist_ok=True)
        external.joinpath("data").mkdir()
        latest = external / "data" / "latest.json"
    else:
        output.mkdir(parents=True, exist_ok=True)
        target = output / component
        target.parent.mkdir(parents=True, exist_ok=True)
        latest = (
            external / "latest.json"
            if component == "data"
            else output / "data" / "latest.json"
        )
        latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_bytes(previous)
    link_factory(external, target)  # type: ignore[operator]
    return latest, previous, sentinel, sentinel_bytes


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink regression")
@pytest.mark.parametrize("component", ["root", "data", "assets", "assets/media"])
def test_output_symlink_is_rejected_without_touching_external_files(
    tmp_path: Path, component: str
) -> None:
    prepare_static(tmp_path)
    if component == "assets/media":
        source = config(tmp_path).media_dir / "post-1" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"\xff\xd8\xffimage")
        save_post(
            tmp_path,
            local_media=(
                LocalMedia(
                    "post",
                    "image",
                    "https://pbs.twimg.com/media/source",
                    "../media/post-1/image.jpg",
                    "image/jpeg",
                ),
            ),
        )
    else:
        save_post(tmp_path)
    latest, previous, sentinel, sentinel_bytes = _seed_output_link_attack(
        tmp_path, component, lambda source, target: os.symlink(source, target)
    )

    with pytest.raises(ValueError, match="dashboard output"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert sentinel.read_bytes() == sentinel_bytes


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("component", ["root", "data", "assets", "assets/media"])
def test_output_junction_is_rejected_without_touching_external_files(
    tmp_path: Path, component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(Path, "is_junction"):
        pytest.skip("Path.is_junction is unavailable")
    prepare_static(tmp_path)
    if component == "assets/media":
        source = config(tmp_path).media_dir / "post-1" / "image.jpg"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"\xff\xd8\xffimage")
        save_post(
            tmp_path,
            local_media=(
                LocalMedia(
                    "post",
                    "image",
                    "https://pbs.twimg.com/media/source",
                    "../media/post-1/image.jpg",
                    "image/jpeg",
                ),
            ),
        )
    else:
        save_post(tmp_path)
    monkeypatch.setattr(dashboard_export, "writer_lock", no_writer_lock)

    def make_junction(source: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(target), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"junctions unavailable: {result.stderr or result.stdout}")

    latest, previous, sentinel, sentinel_bytes = _seed_output_link_attack(
        tmp_path, component, make_junction
    )

    with pytest.raises(ValueError, match="dashboard output"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert sentinel.read_bytes() == sentinel_bytes


def test_configured_output_outside_project_is_rejected(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    settings = config(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside-dashboard"
    outside.joinpath("data").mkdir(parents=True)
    latest = outside / "data" / "latest.json"
    previous = b'{"outside": true}\n'
    latest.write_bytes(previous)
    unsafe_config = SimpleNamespace(
        root=settings.root,
        timezone=settings.timezone,
        keywords=settings.keywords,
        markdown_dir=settings.markdown_dir,
        media_dir=settings.media_dir,
        dashboard_dir=outside,
        dashboard_source_dir=settings.dashboard_source_dir,
    )

    with pytest.raises(ValueError, match="dashboard output"):
        DashboardBuilder(
            unsafe_config,  # type: ignore[arg-type]
            MarkdownStore(settings.markdown_dir),
            clock=lambda: NOW,
        ).build()

    assert latest.read_bytes() == previous


def test_exports_signature_valid_media_by_hash_without_mutating_sources(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    config(tmp_path).dashboard_source_dir.joinpath("assets", "not-copied.js").write_text(
        "private", encoding="utf-8"
    )
    media_specs = (
        ("image-01.JPG", b"\xff\xd8\xffjpeg payload", "image"),
        ("image-02.png", b"\x89PNG\r\n\x1a\npng payload", "image"),
        ("image-03.gif", b"GIF89agif payload", "image"),
        ("poster-01.WEBP", b"RIFF1234WEBPwebp payload", "video_poster"),
    )
    source_files: list[Path] = []
    local_media: list[LocalMedia] = []
    for name, content, kind in media_specs:
        source = config(tmp_path).media_dir / "post-1" / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        source_files.append(source)
        local_media.append(
            LocalMedia(
                "post",
                kind,  # type: ignore[arg-type]
                f"https://pbs.twimg.com/media/{name}",
                f"../media/post-1/{name}",
                "unused/private-metadata",
            )
        )
    markdown_path = save_post(
        tmp_path,
        text="  一段带空格的说明  ",
        local_media=tuple(local_media),
    )
    source_before = {path: path.read_bytes() for path in source_files}
    markdown_before = markdown_path.read_bytes()

    result = build(tmp_path)

    latest = config(tmp_path).dashboard_dir / "data" / "latest.json"
    public_media = json.loads(latest.read_text(encoding="utf-8"))["posts"][0]["media"]
    expected_media = []
    for (name, content, kind), source in zip(media_specs, source_files, strict=True):
        digest = hashlib.sha256(content).hexdigest()
        suffix = Path(name).suffix.lower()
        relative = f"assets/media/{digest}{suffix}"
        copied = config(tmp_path).dashboard_dir.joinpath(*relative.split("/"))
        assert copied.read_bytes() == content
        expected_media.append(
            {"url": relative, "type": kind, "alt": "一段带空格的说明"}
        )
        assert source.read_bytes() == source_before[source]
    assert public_media == expected_media
    assert markdown_path.read_bytes() == markdown_before
    assert result["media_count"] == 4
    assert json.loads(latest.read_text(encoding="utf-8"))["summary"]["media"] == 4

    copied_mtimes = {
        path: path.stat().st_mtime_ns
        for path in config(tmp_path).dashboard_dir.joinpath("assets", "media").iterdir()
    }
    build(tmp_path)
    assert {path: path.stat().st_mtime_ns for path in copied_mtimes} == copied_mtimes

    output_files = {
        path.relative_to(config(tmp_path).dashboard_dir).as_posix()
        for path in config(tmp_path).dashboard_dir.rglob("*")
        if path.is_file()
    }
    assert output_files == {
        ".nojekyll",
        "index.html",
        "assets/styles.css",
        "assets/app.js",
        "data/latest.json",
        "data/2026-08-10T203456+0800.json",
        *{item["url"] for item in expected_media},
    }


def test_missing_unsupported_and_invalid_signature_media_are_skipped(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    media_root = config(tmp_path).media_dir / "post-1"
    media_root.mkdir(parents=True)
    media_root.joinpath("unsupported.txt").write_bytes(b"GIF89a payload")
    media_root.joinpath("invalid.jpg").write_bytes(b"not a jpeg")
    items = tuple(
        LocalMedia(
            "post",
            "image",
            "https://pbs.twimg.com/media/source",
            relative,
            "private/content-type",
        )
        for relative in (
            "../media/post-1/missing.png",
            "../media/post-1/unsupported.txt",
            "../media/post-1/invalid.jpg",
        )
    )
    save_post(tmp_path, text="", local_media=items)

    result = build(tmp_path)

    output_path = result["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["posts"][0]["media"] == []
    assert payload["summary"]["media"] == 0
    assert result["media_count"] == 0
    assert not config(tmp_path).dashboard_dir.joinpath("assets", "media").exists()


def _prepare_single_media_export(
    root: Path, *, duplicate: bool = False
) -> tuple[Path, bytes]:
    prepare_static(root)
    content = b"\xff\xd8\xffsource image"
    source = config(root).media_dir / "post-1" / "image.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    first = LocalMedia(
        "post",
        "image",
        "https://pbs.twimg.com/media/first",
        "../media/post-1/image.jpg",
        "image/jpeg",
    )
    items = (first,)
    if duplicate:
        items += (
            LocalMedia(
                "quoted",
                "video_poster",
                "https://pbs.twimg.com/media/duplicate",
                "../media/post-1/image.jpg",
                "image/jpeg",
            ),
        )
    save_post(root, local_media=items)
    digest = hashlib.sha256(content).hexdigest()
    destination = config(root).dashboard_dir / "assets" / "media" / f"{digest}.jpg"
    return destination, content


@pytest.mark.parametrize("existing_kind", ["corrupt", "directory"])
def test_invalid_existing_hash_asset_preserves_latest(
    tmp_path: Path, existing_kind: str
) -> None:
    destination, _ = _prepare_single_media_export(tmp_path)
    destination.parent.mkdir(parents=True)
    if existing_kind == "corrupt":
        destination.write_bytes(b"\xff\xd8\xffdifferent valid jpeg")
    else:
        destination.mkdir()
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media asset"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_existing_hash_asset_symlink_preserves_latest_and_external_file(
    tmp_path: Path,
) -> None:
    destination, content = _prepare_single_media_export(tmp_path)
    destination.parent.mkdir(parents=True)
    external = tmp_path / "external-image.jpg"
    external.write_bytes(content)
    try:
        os.symlink(external, destination)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media asset"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert external.read_bytes() == content


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_existing_hash_asset_junction_preserves_latest_and_external_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination, _ = _prepare_single_media_export(tmp_path)
    destination.parent.mkdir(parents=True)
    external = tmp_path / "external-asset-directory"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_bytes(b"sentinel")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(destination), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junctions unavailable: {result.stderr or result.stdout}")
    latest, previous = seed_latest(tmp_path)
    monkeypatch.setattr(dashboard_export, "writer_lock", no_writer_lock)

    with pytest.raises(ValueError, match="media asset"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert sentinel.read_bytes() == b"sentinel"


def test_duplicate_media_references_keep_first_entry_once(tmp_path: Path) -> None:
    _, content = _prepare_single_media_export(tmp_path, duplicate=True)

    result = build(tmp_path)

    output_path = result["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["media_count"] == 1
    assert payload["summary"]["media"] == 1
    assert payload["posts"][0]["media"] == [
        {
            "url": f"assets/media/{hashlib.sha256(content).hexdigest()}.jpg",
            "type": "image",
            "alt": "智能体安全动态",
        }
    ]


@pytest.mark.parametrize("relative_path", ["../../outside.jpg", "../media"])
def test_unsafe_media_path_preserves_latest(
    tmp_path: Path, relative_path: str
) -> None:
    prepare_static(tmp_path)
    save_post(
        tmp_path,
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                relative_path,
                "image/jpeg",
            ),
        ),
    )
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media path"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_media_symlink_escape_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffoutside")
    link = config(tmp_path).media_dir / "post-1" / "linked.jpg"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    save_post(
        tmp_path,
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                "../media/post-1/linked.jpg",
                "image/jpeg",
            ),
        ),
    )
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="media path"):
        build(tmp_path)

    assert latest.read_bytes() == previous


def test_fallback_snapshot_and_blank_text_media_alt(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    image = config(tmp_path).media_dir / "post-1" / "image.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xffimage")
    save_post(
        tmp_path,
        author=" Ada ",
        text="   ",
        created_at="2026-08-09T12:00:00+08:00",
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/source",
                "../media/post-1/image.jpg",
                "image/jpeg",
            ),
        ),
    )

    result = build(tmp_path)

    output_path = result["output_path"]
    assert isinstance(output_path, Path)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["fallback_used"] is True
    assert payload["posts"][0]["fallback"] is True
    assert payload["posts"][0]["media"][0]["alt"] == "@Ada 的配图"


def test_non_file_versioned_snapshot_preserves_latest_and_cleans_temp(
    tmp_path: Path,
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)
    dated = latest.parent / "2026-08-10T203456+0800.json"
    dated.mkdir()

    with pytest.raises(ValueError, match="versioned snapshot"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert list(latest.parent.glob("*.tmp")) == []


def test_writer_lock_surrounds_reads_and_all_publication_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    locked = False
    events: list[str] = []

    @contextmanager
    def observed_lock(root: Path):
        nonlocal locked
        assert root == tmp_path
        locked = True
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
            locked = False

    original_write = dashboard_export._write_atomic

    def observed_write(path: Path, content: bytes, guard: object) -> None:
        assert locked
        original_write(path, content, guard)  # type: ignore[arg-type]

    monkeypatch.setattr(dashboard_export, "writer_lock", observed_lock, raising=False)
    monkeypatch.setattr(dashboard_export, "_write_atomic", observed_write)

    build(tmp_path)

    assert events == ["enter", "exit"]


def test_identical_same_second_snapshot_reuses_versioned_file(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    first = build(tmp_path)
    dated = first["dated_snapshot_path"]
    assert isinstance(dated, Path)
    old_timestamp = 1_000_000_000
    os.utime(dated, ns=(old_timestamp, old_timestamp))

    second = build(tmp_path)

    assert second["dated_snapshot_path"] == dated
    assert dated.stat().st_mtime_ns == old_timestamp


def test_different_same_second_snapshot_uses_payload_hash_suffix(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, text="first snapshot")
    first = build(tmp_path)
    first_path = first["dated_snapshot_path"]
    assert isinstance(first_path, Path)
    first_bytes = first_path.read_bytes()
    save_post(tmp_path, text="second snapshot")

    second = build(tmp_path)

    second_path = second["dated_snapshot_path"]
    output_path = second["output_path"]
    assert isinstance(second_path, Path)
    assert isinstance(output_path, Path)
    second_bytes = output_path.read_bytes()
    digest = hashlib.sha256(second_bytes).hexdigest()[:12]
    assert second_path.name == f"2026-08-10T203456+0800-{digest}.json"
    assert second_path.read_bytes() == second_bytes
    assert first_path.read_bytes() == first_bytes


def test_conflicting_hashed_version_preserves_latest(tmp_path: Path) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path, text="first snapshot")
    build(tmp_path)
    save_post(tmp_path, text="second snapshot")
    second = build(tmp_path)
    second_path = second["dated_snapshot_path"]
    assert isinstance(second_path, Path)
    second_path.write_bytes(b"conflicting bytes")
    latest, previous = seed_latest(tmp_path)

    with pytest.raises(ValueError, match="versioned snapshot"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert second_path.read_bytes() == b"conflicting bytes"


def test_latest_atomic_write_failure_preserves_previous_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_static(tmp_path)
    save_post(tmp_path)
    latest, previous = seed_latest(tmp_path)
    original_replace = dashboard_export.os.replace

    def fail_latest(source: object, destination: object) -> None:
        if Path(destination) == latest:
            raise OSError("injected latest failure")
        original_replace(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(dashboard_export.os, "replace", fail_latest)

    with pytest.raises(OSError, match="injected latest failure"):
        build(tmp_path)

    assert latest.read_bytes() == previous
    assert list(latest.parent.glob(".xrag-*.tmp")) == []
