# X-RAG Local Media Archiving Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve complete X post text, quoted-post content, images, and video posters in readable local Markdown while keeping collection resilient, secure, searchable, and scheduled at 10:00.

**Architecture:** Extend the normalized post model and OpenCLI parser, then add a focused `MediaStore` that downloads only validated X-hosted images. `XragService` coordinates media archival before Markdown and vector writes; `MarkdownStore` renders readable sections with hidden round-trip markers and remains compatible with existing files.

**Tech Stack:** Python 3.14, dataclasses, stdlib `urllib.request`, PyYAML, Typer, ChromaDB, sentence-transformers, pytest, Windows Task Scheduler, WSL Ubuntu.

---

Run all Python and pytest commands from `/mnt/c/Users/1/Documents/X工作流/.worktrees/x-rag` inside WSL with the existing `.venv`.

## File map

- Create `src/xrag/media_store.py`: secure image and video-poster download, validation, atomic writes, structured failures.
- Create `tests/test_media_store.py`: all MediaStore behavior with injected fake HTTP responses.
- Modify `src/xrag/models.py`: quoted-post and local-media value objects plus searchable text.
- Modify `src/xrag/opencli.py`: parse `media_posters` and first-level `quoted_tweet`.
- Modify `src/xrag/markdown_store.py`: new readable Markdown format and old-format compatibility.
- Modify `src/xrag/importers.py`: canonical Markdown text extraction and new optional fields.
- Modify `src/xrag/service.py`: media orchestration and non-fatal media error accounting.
- Modify `src/xrag/cli.py`: construct and inject MediaStore.
- Modify `src/xrag/config.py`: expose `media_dir`.
- Modify `src/xrag/vector_store.py`: index main and quoted text, not Markdown decoration.
- Modify `config/keywords.yaml`: four approved AI/Web3 queries and limit 10.
- Modify `README.md`: storage layout, media behavior, keyword groups, and troubleshooting.
- Modify related existing test files and add fixture coverage.

### Task 1: Extend normalized models and OpenCLI parsing

**Files:**
- Modify: `src/xrag/models.py`
- Modify: `src/xrag/opencli.py`
- Modify: `tests/fixtures/opencli-search.yaml`
- Modify: `tests/test_opencli.py`
- Create: `tests/test_models.py`

- [ ] **Step 1: Write failing model and parser tests**

Add this model test to `tests/test_models.py`:

```python
from xrag.models import Post, QuotedPost


def test_searchable_text_includes_one_quoted_post_without_markdown_decoration() -> None:
    post = Post(
        id="123",
        author="main",
        text="main body",
        created_at="2026-08-10T00:00:00Z",
        url="https://x.com/main/status/123",
        quoted_post=QuotedPost(
            id="456",
            author="quoted",
            text="quoted body",
            created_at="2026-08-09T00:00:00Z",
            url="https://x.com/quoted/status/456",
        ),
    )

    assert post.searchable_text == "main body\n\nquoted body"
```

Extend `tests/fixtures/opencli-search.yaml` with `media_posters` and a `quoted_tweet` containing `id`, `author`, `text`, `created_at`, `url`, `media_urls`, and `media_posters`. Extend `test_parse_search_yaml_normalizes_a_search_result` with these exact assertions:

```python
assert post.media_posters == (
    "https://pbs.twimg.com/media/DDR5-poster.jpg",
)
assert post.quoted_post is not None
assert post.quoted_post.id == "2084640002085130000"
assert post.quoted_post.author == "quoted_author"
assert post.quoted_post.text == "quoted text"
assert post.quoted_post.media_urls == (
    "https://pbs.twimg.com/media/quoted-image.jpg",
)
assert post.quoted_post.media_posters == (
    "https://pbs.twimg.com/media/quoted-poster.jpg",
)
```

Add a parser test proving malformed or nested quoted data is bounded:

```python
def test_parser_ignores_invalid_and_nested_quoted_posts() -> None:
    posts = parse_search_yaml(
        """
- id: "1"
  text: main
  quoted_tweet:
    id: "2"
    author: quoted
    text: quoted body
    quoted_tweet:
      id: "3"
      text: must not recurse
- id: "4"
  text: second
  quoted_tweet:
    id: not-numeric
    text: ignored
""",
        "AI",
    )

    assert posts[0].quoted_post is not None
    assert not hasattr(posts[0].quoted_post, "quoted_post")
    assert posts[1].quoted_post is None
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_models.py tests/test_opencli.py -v
```

Expected: collection fails because `QuotedPost`, `Post.searchable_text`, `media_posters`, and `quoted_post` do not exist.

- [ ] **Step 3: Implement the model types**

Add these immutable types before `Post` in `src/xrag/models.py`, then add the three new fields and property to `Post`:

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class QuotedPost:
    id: str
    author: str
    text: str
    created_at: str
    url: str
    media_urls: tuple[str, ...] = ()
    media_posters: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalMedia:
    owner: Literal["post", "quoted"]
    kind: Literal["image", "video_poster"]
    source_url: str
    relative_path: str
    content_type: str
```

The completed additions inside `Post` are:

```python
    media_posters: tuple[str, ...] = ()
    quoted_post: QuotedPost | None = None
    local_media: tuple[LocalMedia, ...] = ()

    @property
    def searchable_text(self) -> str:
        parts = [self.text.strip()]
        if self.quoted_post is not None and self.quoted_post.text.strip():
            parts.append(self.quoted_post.text.strip())
        return "\n\n".join(part for part in parts if part)
```

- [ ] **Step 4: Implement first-level OpenCLI normalization**

Import `QuotedPost`, pass `media_posters` and `quoted_post` when constructing `Post`, and add this helper to `src/xrag/opencli.py`:

```python
def _quoted_post(value: object) -> QuotedPost | None:
    if not isinstance(value, Mapping):
        return None
    post_id = _identifier(value.get("id"))
    text = _string(value.get("text"))
    if not post_id or not text:
        return None
    return QuotedPost(
        id=post_id,
        author=_string(value.get("author")) or "unknown",
        text=text,
        created_at=_timestamp(value.get("created_at")),
        url=_string(value.get("url")) or f"https://x.com/i/status/{post_id}",
        media_urls=_media_urls(value.get("media_urls")),
        media_posters=_media_urls(value.get("media_posters")),
    )
```

Use these constructor arguments in `_normalize_post`:

```python
        media_urls=_media_urls(row.get("media_urls")),
        media_posters=_media_urls(row.get("media_posters")),
        quoted_post=_quoted_post(row.get("quoted_tweet")),
```

- [ ] **Step 5: Run focused and existing parser tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_models.py tests/test_opencli.py -v
```

Expected: all model and OpenCLI tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/xrag/models.py src/xrag/opencli.py tests/fixtures/opencli-search.yaml tests/test_opencli.py tests/test_models.py
git commit -m "feat: preserve x media and quoted post metadata"
```

### Task 2: Add a secure, atomic MediaStore

**Files:**
- Create: `src/xrag/media_store.py`
- Create: `tests/test_media_store.py`
- Modify: `src/xrag/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for successful image and poster archival**

Create a fake response in `tests/test_media_store.py` with `read`, `headers`, `geturl`, and context-manager methods. Add a test that archives one top-level image and one quoted video poster:

```python
from io import BytesIO
from pathlib import Path

from xrag.media_store import MediaStore
from xrag.models import Post, QuotedPost


class FakeResponse:
    def __init__(self, payload: bytes, content_type: str, url: str) -> None:
        self._stream = BytesIO(payload)
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}
        self._url = url

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_archive_downloads_image_and_quoted_video_poster(tmp_path: Path) -> None:
    payloads = {
        "https://pbs.twimg.com/media/image": (b"jpeg-image", "image/jpeg"),
        "https://pbs.twimg.com/media/poster": (b"png-poster", "image/png"),
    }

    def open_url(request: object, timeout: float) -> FakeResponse:
        url = request.full_url  # type: ignore[attr-defined]
        payload, content_type = payloads[url]
        return FakeResponse(payload, content_type, url)

    post = Post(
        "123", "main", "body", "", "https://x.com/i/status/123",
        media_urls=("https://pbs.twimg.com/media/image",),
        quoted_post=QuotedPost(
            "456", "quoted", "quote", "", "https://x.com/i/status/456",
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
```

- [ ] **Step 2: Write failing security and cleanup tests**

Add parameterized tests proving that HTTP URLs, non-`pbs.twimg.com` hosts, redirected final URLs, unsupported MIME types, and files larger than the configured maximum return one `MediaFailure` and create no canonical or temporary file. Add a redirect-handler test proving a redirect to a non-allowlisted host is rejected before a second request is constructed or sent. Add this non-video assertion:

```python
def test_archive_never_downloads_video_body(tmp_path: Path) -> None:
    calls: list[str] = []

    def open_url(request: object, timeout: float) -> FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        raise AssertionError("video body must not be requested")

    post = Post(
        "123", "main", "body", "", "https://x.com/i/status/123",
        media_urls=("https://video.twimg.com/ext_tw_video/file.mp4",),
    )
    result = MediaStore(tmp_path / "media", open_url=open_url).archive(post)

    assert result.post.local_media == ()
    assert result.failures == ()
    assert calls == []
```

Add a second-archive test that calls `archive` with the `Post` returned by the first archive, injects an opener which raises if called, and verifies the existing valid local file is reused only because its `LocalMedia` entry has the same `owner`, `kind`, and `source_url`. Add a mismatch test proving a file at the expected ordinal path is not reused for a different source URL.

- [ ] **Step 3: Run MediaStore tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_media_store.py -v
```

Expected: import fails because `xrag.media_store` does not exist.

- [ ] **Step 4: Implement MediaStore public types and archive flow**

Create `src/xrag/media_store.py` with these public types and constants:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import os
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import LocalMedia, Post


_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_ALLOWED_HOSTS = frozenset({"pbs.twimg.com"})


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
```

`MediaStore.__init__` accepts `directory`, `open_url`, `timeout=20.0`, and `max_bytes=25 * 1024 * 1024`. The default opener must use stdlib `build_opener` with a custom `HTTPRedirectHandler` that validates every redirect target before following it and raises for any target that is not HTTPS on `pbs.twimg.com`. After opening, verify `response.geturl()` again as defense in depth. A final-URL-only check is insufficient because it would contact the untrusted redirect target before rejecting it.

Implement `archive` by enumerating these sources in stable order:

```python
sources = [
    ("post", "image", post.media_urls),
    ("post", "video_poster", post.media_posters),
]
if post.quoted_post is not None:
    sources.extend([
        ("quoted", "image", post.quoted_post.media_urls),
        ("quoted", "video_poster", post.quoted_post.media_posters),
    ])
```

Skip every `media_urls` item whose host is `video.twimg.com`. Before downloading a source, look for an entry in `post.local_media` with the exact same `owner`, `kind`, and `source_url`; reuse it only when its resolved path remains inside `data/media/<post_id>/`, its MIME type is supported, and the file exists. Never reuse a file merely because its ordinal filename exists. For accepted downloads, stream in 64 KiB chunks, enforce `Content-Length` before reading and `max_bytes` while reading, derive the extension from `_CONTENT_TYPES`, flush and `os.fsync`, then call `os.replace`. Return `replace(post, local_media=tuple(saved))` plus all structured failures.

- [ ] **Step 5: Add config path and verify tests GREEN**

Add this property to `AppConfig`:

```python
    @property
    def media_dir(self) -> Path:
        return self.root / "data" / "media"
```

Add `assert config.media_dir == tmp_path / "data" / "media"` to `test_load_config_reads_valid_configuration`.

Run:

```bash
.venv/bin/python -m pytest tests/test_media_store.py tests/test_config.py -v
```

Expected: all tests pass and no `*.tmp` files remain under test directories.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/xrag/media_store.py src/xrag/config.py tests/test_media_store.py tests/test_config.py
git commit -m "feat: archive x images and video posters locally"
```

### Task 3: Render readable Markdown and preserve round trips

**Files:**
- Modify: `src/xrag/markdown_store.py`
- Modify: `src/xrag/importers.py`
- Modify: `tests/test_markdown_store.py`
- Modify: `tests/test_importers.py`

- [ ] **Step 1: Write failing readable-format tests**

Extend the Markdown test post with `media_posters`, `quoted_post`, and `local_media`. Add assertions for:

```python
content = path.read_text(encoding="utf-8")
assert content.count("# @张三的推文") == 1
assert "## 正文" in content
assert "<!-- xrag:text:start -->" in content
assert "<!-- xrag:text:end -->" in content
assert "![图片 1](../media/123/image-01.jpg)" in content
assert "![视频封面 1](../media/123/video-poster-01.jpg)" in content
assert "## 引用推文" in content
assert "> @quoted：quoted body" in content
assert "[查看 X 原文](https://x.com/example/status/123)" in content
assert store.read(path) == make_post(text="第一段。\n\n第二段。")
```

Add an old-format compatibility test using the current front matter fields and a raw body; assert new optional fields default to empty and `text` remains the entire raw body.

Add an importer test that imports a new canonical Markdown file and asserts only the text between the hidden text markers becomes `Post.text`; headings and media links must not enter `Post.text`.

Add a store lookup test proving `MarkdownStore.get(post_id)` returns the canonical post when present and `None` when absent, while applying the same safe-ID validation as `upsert`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_markdown_store.py tests/test_importers.py -v
```

Expected: assertions fail because the old store writes only front matter plus raw text.

- [ ] **Step 3: Implement optional metadata serialization**

Keep the current required front matter fields. Add optional defaults:

```python
_OPTIONAL_FRONT_MATTER_DEFAULTS: dict[str, object] = {
    "media_posters": [],
    "local_media": [],
    "quoted_tweet": None,
}
_TEXT_START = "<!-- xrag:text:start -->"
_TEXT_END = "<!-- xrag:text:end -->"
```

Serialize `QuotedPost` to a mapping and each `LocalMedia` to a mapping. Parse them with strict type checks; malformed optional objects must raise `ValueError` rather than silently corrupt a canonical file. Missing optional fields in old files use the defaults above. Add `MarkdownStore.get(post_id) -> Post | None`, implemented through `_path_for`, so callers can safely load an existing canonical mapping without constructing paths themselves.

- [ ] **Step 4: Implement readable body rendering and extraction**

Add this public helper for both MarkdownStore and importers:

```python
def extract_body_text(body: str) -> str:
    if _TEXT_START not in body and _TEXT_END not in body:
        return body.strip()
    start = body.find(_TEXT_START)
    end = body.find(_TEXT_END, start + len(_TEXT_START))
    if start < 0 or end < 0:
        raise ValueError("invalid canonical Markdown text markers")
    value = body[start + len(_TEXT_START) : end]
    return value.strip("\n")
```

Render the body in this order: title, text heading and markers, media heading, quoted heading, original-link footer. Only render local items from `local_media`; pair each rendered item with its `source_url`. Use `owner` to place quoted media in the quoted section.

Update `MarkdownStore.read` to call `extract_body_text`. Update `importers._load_markdown` to call the same helper instead of assigning the whole decorated body to `row["text"]`.

- [ ] **Step 5: Run Markdown, importer, rebuild, and offline-flow tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_markdown_store.py tests/test_importers.py tests/test_atomic_rebuild.py tests/test_offline_flow.py -v
```

Expected: all tests pass; old canonical fixtures still rebuild without network access.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/xrag/markdown_store.py src/xrag/importers.py tests/test_markdown_store.py tests/test_importers.py
git commit -m "feat: render readable markdown with local media"
```

### Task 4: Integrate media archival into collect and import

**Files:**
- Modify: `src/xrag/service.py`
- Modify: `src/xrag/cli.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing service resilience tests**

Create a fake media store in `tests/test_service.py`:

```python
from xrag.media_store import MediaArchiveResult, MediaFailure


class Media:
    def __init__(self, result: MediaArchiveResult | None = None, failure: Exception | None = None) -> None:
        self.result = result
        self.failure = failure
        self.archived: list[str] = []

    def archive(self, item: Post) -> MediaArchiveResult:
        self.archived.append(item.id)
        if self.failure is not None:
            raise self.failure
        return self.result or MediaArchiveResult(item, ())
```

Add tests proving:

1. an archived `Post` is passed to both MarkdownStore and vectors;
2. each `MediaFailure` increments `errors`, writes a sanitized log record, but leaves `stored == 1`;
3. an unexpected `MediaStore.archive` exception also increments `errors` and still stores/indexes the original post;
4. imports use the same media path while non-X URLs become failures rather than network requests.
5. recollecting a post loads its existing canonical `local_media` mappings before archival, so an unchanged source URL reuses the existing file while a changed URL is downloaded and atomically replaces only its own ordinal target.

The central expected count for one successful post with one media failure is:

```python
assert result == {"found": 1, "stored": 1, "chunks": 2, "errors": 1}
```

- [ ] **Step 2: Run service and CLI tests to verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_service.py tests/test_cli.py -v
```

Expected: tests fail because `XragService` does not accept a media store and `build_service` does not construct one.

- [ ] **Step 3: Add optional media dependency and a non-fatal helper**

Add `media: Any | None = None` as a keyword-only constructor argument and store it as `self.media`. Before calling `archive`, use `self.markdown.get(item.id)` and `dataclasses.replace` to merge canonical `local_media` mappings into the incoming post; keep incoming mappings first and deduplicate exact entries. This preserves URL-to-file identity across repeated collection without trusting ordinal filenames alone. Add this helper:

```python
    def _archive_media(self, item: Post, counts: dict[str, int]) -> Post:
        if self.media is None:
            return item
        try:
            result = self.media.archive(item)
        except Exception as error:
            counts["errors"] += 1
            self._log_error(
                "media",
                self._post_id(item),
                error,
                fixed_message="media archival failed",
            )
            return item
        for failure in result.failures:
            counts["errors"] += 1
            self._log_error(
                "media",
                f"{self._post_id(item)}:{failure.owner}:{failure.kind}",
                RuntimeError(failure.reason),
                fixed_message=failure.reason,
                error_name=failure.error_name,
            )
        return result.post
```

Call `_archive_media` immediately before each `markdown.upsert` in both `collect` and `import_path`. Pass the returned post to MarkdownStore and vectors. Do not invoke it from `rebuild`, `search`, or `status`.

- [ ] **Step 4: Wire MediaStore in the CLI**

Import `MediaStore` and construct the service with:

```python
    media = MediaStore(config.media_dir)
    return XragService(
        config,
        OpenCLIClient(),
        markdown,
        None,
        media=media,
        vector_factory=vector_factory,
        rebuild_factory=vector_factory,
    )
```

Update CLI construction tests to assert the MediaStore directory equals `config.media_dir`.

- [ ] **Step 5: Run service, CLI, locking, and lifecycle tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_service.py tests/test_cli.py tests/test_locking.py tests/test_vector_lifecycle.py -v
```

Expected: all tests pass; media work occurs under the existing writer lock and vector lifecycle remains serialized.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/xrag/service.py src/xrag/cli.py tests/test_service.py tests/test_cli.py
git commit -m "feat: archive media without blocking post storage"
```

### Task 5: Index quoted text without indexing Markdown decoration

**Files:**
- Modify: `src/xrag/vector_store.py`
- Modify: `tests/test_vector_store.py`
- Modify: `tests/test_offline_flow.py`

- [ ] **Step 1: Write a failing vector document test**

Create a post with `text="main body"` and a quoted post with `text="quoted searchable body"`. Index it, inspect the fake collection documents, and assert:

```python
assert "main body" in indexed_text
assert "quoted searchable body" in indexed_text
assert "## 正文" not in indexed_text
assert "![图片" not in indexed_text
assert "pbs.twimg.com" not in indexed_text
```

- [ ] **Step 2: Run focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_vector_store.py -v
```

Expected: quoted text is absent because `index_post` chunks only `post.text`.

- [ ] **Step 3: Use the normalized searchable text**

Change the first line of `VectorStore.index_post` from:

```python
chunks = chunk_text(post.text, self.max_chars, self.overlap)
```

to:

```python
chunks = chunk_text(post.searchable_text, self.max_chars, self.overlap)
```

No Markdown file reads or network calls belong in VectorStore.

- [ ] **Step 4: Run vector and offline integration tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_vector_store.py tests/test_offline_flow.py tests/test_vector_lifecycle.py -v
```

Expected: all tests pass and quoted text is searchable.

- [ ] **Step 5: Commit Task 5**

```bash
git add src/xrag/vector_store.py tests/test_vector_store.py tests/test_offline_flow.py
git commit -m "feat: index quoted x post text"
```

### Task 6: Apply the four approved keyword groups

**Files:**
- Modify: `config/keywords.yaml`
- Modify: `tests/test_config.py`
- Modify: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Add a failing production-config test**

Add a test that loads the repository config and asserts `limit_per_keyword == 10`, `len(keywords) == 4`, and the four exact query strings from the approved design. Also assert schedule time remains `10:00` and timezone remains `Asia/Singapore`.

- [ ] **Step 2: Run the config tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_scheduler_scripts.py -v
```

Expected: the production-config test fails because the current config contains one keyword and limit 50.

- [ ] **Step 3: Replace the production keyword list**

Write this configuration exactly, using YAML single quotes so the embedded double quotes reach OpenCLI unchanged:

```yaml
schedule:
  enabled: true
  time: "10:00"
  timezone: Asia/Singapore
collection:
  limit_per_keyword: 10
  delay_seconds: 10
keywords:
  - '"Autonomous AI Agents" OR 自主智能体 OR "Rogue AI Agents" OR "Agent Security" OR "AI Safety Evaluation" OR "AI Cybersecurity"'
  - '"World Models" OR 世界模型 OR "Open-weight Models" OR AGI OR "Intelligence Explosion" OR "Embodied AI" OR 具身智能 OR "Humanoid Robots"'
  - 'RWA OR 现实资产代币化 OR "Tokenized Stocks" OR "Stablecoin Payments" OR "Solana RWA"'
  - '"Prediction Markets" OR "AI Agents Crypto" OR x402 OR "On-chain Perps" OR "Crypto ETF" OR MiCA OR "CLARITY Act" OR 加密监管'
embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

- [ ] **Step 4: Run config and scheduler tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_scheduler_scripts.py -v
```

Expected: all tests pass; scheduler runner still invokes `collect --all` once.

- [ ] **Step 5: Commit Task 6**

```bash
git add config/keywords.yaml tests/test_config.py tests/test_scheduler_scripts.py
git commit -m "config: collect four ai and web3 topic groups"
```

### Task 7: Update documentation and operator guidance

**Files:**
- Modify: `README.md`
- Modify: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Write a failing documentation contract test**

Add assertions that README contains:

```python
assert "data/media/<推文ID>/" in readme
assert "视频只下载封面" in readme
assert "正文只有短链接" in readme
assert "每组每天采集 10 条" in readme
assert "图片下载失败" in readme
```

- [ ] **Step 2: Run the documentation test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler_scripts.py -v
```

Expected: README contract assertions fail.

- [ ] **Step 3: Document layout, behavior, and recovery**

Update README with:

- `data/markdown/<id>.md` and `data/media/<id>/` layout;
- the four keyword groups and 10-result limit;
- local image rendering and original X links;
- video poster-only behavior;
- retained short-link-only posts;
- media errors in `logs/errors.jsonl` that do not block text storage;
- a verification command that lists local media:

```bash
find data/media -type f -maxdepth 2 -print
```

- the existing offline `status`, `search`, and `rebuild` commands.

- [ ] **Step 4: Run documentation and CLI tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_scheduler_scripts.py tests/test_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 7**

```bash
git add README.md tests/test_scheduler_scripts.py
git commit -m "docs: explain local x media archives"
```

### Task 8: Full regression and offline acceptance

**Files:**
- Test only; modify code only if a failing test reveals a requirement violation, using a new failing regression test first.

- [ ] **Step 1: Run dependency validation**

```bash
.venv/bin/python -m pip check
```

Expected: `No broken requirements found.`

- [ ] **Step 2: Run the complete suite with a visible summary**

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m pytest -o addopts=
```

Expected: all tests pass; only documented platform skips remain.

- [ ] **Step 3: Run a clean offline status and rebuild**

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag status
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag rebuild
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag search "自主智能体 安全" --top 5
```

Expected: status and rebuild report zero document errors; search exits successfully without a network model download.

- [ ] **Step 4: Verify no credential or runtime data is tracked**

```bash
git status --short
git ls-files data .venv logs
rg -n "auth_token|ct0|Authorization:|Bearer " --glob '!docs/superpowers/**' .
```

Expected: runtime directories are not tracked and no credential value appears. Identifier names in tests or redaction code are acceptable only when they contain no secret value.

- [ ] **Step 5: Commit any test-only corrections, otherwise leave the tree clean**

If a regression test required a correction, stage only the named correction files and commit:

```bash
git add src/xrag tests
git commit -m "fix: satisfy local media acceptance checks"
```

If no correction was required, do not create an empty commit.

### Task 9: Live X collection and scheduled-task acceptance

**Files:**
- Runtime data only under ignored `data/` and `logs/`; no source-file edits expected.

- [ ] **Step 1: Confirm OpenCLI browser bridge**

```bash
/home/zyaire/.local/bin/opencli doctor
```

Expected: daemon and Browser Bridge extension are connected.

- [ ] **Step 2: Run the four approved groups**

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag collect --all
```

Expected: four summary lines, each with `found`, `stored`, `chunks`, and `errors`; media errors may be nonzero without reducing stored text counts.

- [ ] **Step 3: Verify readable text and at least one local media file**

```bash
find data/media -mindepth 2 -maxdepth 2 -type f -print
rg -n "^## 正文$|^## 媒体$|^!\[图片|^!\[视频封面" data/markdown
```

Expected: at least one file under `data/media/<id>/`, and its Markdown contains a relative `../media/<id>/...` reference. If the live 40-result sample contains no media, run one direct OpenCLI search to confirm the absence and report the sample limitation instead of fabricating success.

- [ ] **Step 4: Verify search after live collection**

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag search "RWA 稳定币支付" --top 5
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/xrag search "世界模型 具身智能" --top 5
```

Expected: results include source text, X URLs, and local Markdown paths.

- [ ] **Step 5: Check the Windows daily task without triggering a second collection**

Run from PowerShell:

```powershell
Get-ScheduledTask -TaskName 'X-RAG Daily Collection' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'X-RAG Daily Collection' | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

Expected: task exists, is ready when idle, and its next run is 10:00 local time.

- [ ] **Step 6: Verify the Git tree remains clean**

```bash
git status --short --branch
```

Expected: `codex/x-rag-media` has no uncommitted tracked changes; runtime media and Markdown remain ignored.

### Task 10: Review, push, and create the follow-up PR

**Files:**
- No planned file modifications.

- [ ] **Step 1: Review the complete diff against the design**

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: no whitespace errors; commits correspond to model parsing, MediaStore, Markdown, service integration, indexing, keywords, and docs.

- [ ] **Step 2: Re-run the complete offline suite immediately before push**

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/bin/python -m pytest -o addopts=
```

Expected: all tests pass.

- [ ] **Step 3: Push the feature branch**

```bash
git -c credential.interactive=never -c credential.username=bitesiling-coder push -u origin codex/x-rag-media
```

Expected: branch tracks `origin/codex/x-rag-media`.

- [ ] **Step 4: Create a PR targeting main**

Title:

```text
feat: archive X images and video posters locally
```

Body:

```markdown
## 概要

- 保存完整推文与第一层引用推文正文
- 安全下载 X 图片和视频封面到本地
- 在 Markdown 中显示本地媒体并保留原始链接
- 将每日采集缩减为两组 AI 和两组 Web3 主题

## 验证

- 完整离线测试通过
- 实际 X 采集、媒体落盘、Markdown 预览和语义检索通过
- Windows 每日上午 10:00 计划任务保持正常
```

Expected: PR URL is returned and the base/head pair is `main <- codex/x-rag-media`.
