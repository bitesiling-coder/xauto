# X RAG Knowledge Base Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a free local CLI that collects X posts through the existing OpenCLI browser bridge, stores canonical Markdown files, indexes them in ChromaDB, supports file imports and semantic retrieval, and schedules collection every day at 10:00 Asia/Singapore time.

**Architecture:** Markdown files are the canonical record and ChromaDB is a disposable semantic index. Small Python modules isolate OpenCLI execution, parsing, Markdown persistence, import normalization, chunking, vector operations, orchestration, locking, and CLI presentation. Windows Task Scheduler calls one WSL runner script; credentials remain owned by OpenCLI and are never copied into this project.

**Tech Stack:** Ubuntu WSL, Python 3.14, Typer, PyYAML, portalocker, ChromaDB, Sentence Transformers, `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, pytest, Windows Task Scheduler.

---

## File map

- `pyproject.toml`: packaging, runtime dependencies, `xrag` console entry point, pytest settings.
- `.gitignore`: excludes the WSL virtual environment, collected data, vector files, caches, and logs.
- `config/keywords.yaml`: committed default collection and schedule configuration.
- `src/xrag/models.py`: immutable normalized `Post` and `SearchHit` value objects.
- `src/xrag/config.py`: validated YAML configuration and project path resolution.
- `src/xrag/opencli.py`: subprocess boundary and OpenCLI YAML normalization.
- `src/xrag/markdown_store.py`: Markdown front matter serialization, parsing, upsert, and iteration.
- `src/xrag/importers.py`: YAML, JSON, and Markdown import adapters.
- `src/xrag/chunking.py`: deterministic Chinese-friendly paragraph chunking.
- `src/xrag/vector_store.py`: Chroma persistence and local Sentence Transformer embedding adapter.
- `src/xrag/locking.py`: cross-process writer lock.
- `src/xrag/service.py`: collect, import, index, search, rebuild, and status use cases.
- `src/xrag/cli.py`: Typer commands and user-facing output/exit codes.
- `scripts/run-daily.sh`: non-interactive WSL scheduled collector.
- `scripts/install-schedule.ps1`: idempotent Windows scheduled-task installer.
- `README.md`: Chinese installation and operating guide.
- `tests/`: unit and integration tests mirroring the modules above.

### Task 1: Package skeleton and validated configuration

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config/keywords.yaml`
- Create: `src/xrag/__init__.py`
- Create: `src/xrag/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
# tests/test_config.py
from pathlib import Path

import pytest

from xrag.config import AppConfig, load_config


def test_load_config_resolves_project_paths_and_schedule(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "keywords.yaml").write_text(
        """
schedule:
  enabled: true
  time: "10:00"
  timezone: "Asia/Singapore"
collection:
  limit_per_keyword: 50
  delay_seconds: 10
keywords: [人工智能, AI 视频]
embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
""".strip(),
        encoding="utf-8",
    )

    config = load_config(tmp_path)

    assert config.schedule_time == "10:00"
    assert config.keywords == ("人工智能", "AI 视频")
    assert config.markdown_dir == tmp_path / "data" / "markdown"


def test_load_config_rejects_invalid_time(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "keywords.yaml").write_text(
        "schedule: {enabled: true, time: '25:00', timezone: Asia/Singapore}\n"
        "collection: {limit_per_keyword: 50, delay_seconds: 10}\n"
        "keywords: [AI]\nembedding: {model: model}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="HH:MM"):
        load_config(tmp_path)
```

- [ ] **Step 2: Create packaging metadata, install the editable package, and verify RED**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "xrag"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "chromadb>=1.0,<2",
  "portalocker>=3,<4",
  "PyYAML>=6,<7",
  "sentence-transformers>=5,<6",
  "typer>=0.16,<1",
]

[project.optional-dependencies]
dev = ["pytest>=8,<10"]

[project.scripts]
xrag = "xrag.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/xrag"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

Run:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest tests/test_config.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'xrag.config'`.

- [ ] **Step 3: Add the minimal configuration implementation and defaults**

```python
# src/xrag/config.py
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml


@dataclass(frozen=True)
class AppConfig:
    root: Path
    schedule_enabled: bool
    schedule_time: str
    timezone: str
    limit_per_keyword: int
    delay_seconds: int
    keywords: tuple[str, ...]
    embedding_model: str

    @property
    def markdown_dir(self) -> Path:
        return self.root / "data" / "markdown"

    @property
    def import_dir(self) -> Path:
        return self.root / "data" / "imports"

    @property
    def chroma_dir(self) -> Path:
        return self.root / "data" / "chroma"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


def load_config(root: Path) -> AppConfig:
    root = root.resolve()
    raw = yaml.safe_load((root / "config" / "keywords.yaml").read_text(encoding="utf-8"))
    schedule_time = str(raw["schedule"]["time"])
    try:
        datetime.strptime(schedule_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("schedule.time must use valid HH:MM format") from exc
    keywords = tuple(dict.fromkeys(str(item).strip() for item in raw["keywords"] if str(item).strip()))
    if not keywords:
        raise ValueError("keywords must contain at least one non-empty value")
    return AppConfig(
        root=root,
        schedule_enabled=bool(raw["schedule"]["enabled"]),
        schedule_time=schedule_time,
        timezone=str(raw["schedule"]["timezone"]),
        limit_per_keyword=int(raw["collection"]["limit_per_keyword"]),
        delay_seconds=int(raw["collection"]["delay_seconds"]),
        keywords=keywords,
        embedding_model=str(raw["embedding"]["model"]),
    )
```

```yaml
# config/keywords.yaml
schedule:
  enabled: true
  time: "10:00"
  timezone: "Asia/Singapore"
collection:
  limit_per_keyword: 50
  delay_seconds: 10
keywords:
  - 人工智能
embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

```gitignore
# .gitignore
.venv/
__pycache__/
.pytest_cache/
*.pyc
data/markdown/
data/imports/
data/chroma/
logs/
.xrag.lock
```

`src/xrag/__init__.py` remains an empty package marker.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_config.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore config/keywords.yaml src/xrag/__init__.py src/xrag/config.py tests/test_config.py
git commit -m "feat: scaffold xrag and validate configuration"
```

### Task 2: Normalize OpenCLI search results

**Files:**
- Create: `src/xrag/models.py`
- Create: `src/xrag/opencli.py`
- Test: `tests/fixtures/opencli-search.yaml`
- Test: `tests/test_opencli.py`

- [ ] **Step 1: Write failing parser and command tests**

```yaml
# tests/fixtures/opencli-search.yaml
- id: "2084640002085130466"
  author: 0xQiYan
  bio: ""
  text: |-
    18岁小伙重金囤DDR5内存，押注2040年价格翻5倍
  created_at: Tue Aug 04 13:58:01 +0000 2026
  likes: 5
  views: "1739"
  url: https://x.com/i/status/2084640002085130466
  has_media: true
  media_urls:
    - https://pbs.twimg.com/media/example.jpg
  media_posters:
    - https://pbs.twimg.com/media/example.jpg
  card: null
  quoted_tweet: null
```

```python
# tests/test_opencli.py
from pathlib import Path
from subprocess import CompletedProcess

from xrag.opencli import OpenCLIClient, parse_search_yaml


def test_parse_search_yaml_normalizes_post_fields():
    payload = Path("tests/fixtures/opencli-search.yaml").read_text(encoding="utf-8")

    post = parse_search_yaml(payload, "DDR5")[0]

    assert post.id == "2084640002085130466"
    assert post.views == 1739
    assert post.source_keywords == ("DDR5",)
    assert post.media_urls == ("https://pbs.twimg.com/media/example.jpg",)


def test_search_invokes_opencli_with_limit():
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, stdout="[]\n", stderr="")

    result = OpenCLIClient(run=run).search("AI 视频", limit=50)

    assert result == []
    assert calls[0][0] == ["opencli", "twitter", "search", "AI 视频", "--limit", "50", "-f", "yaml"]
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_opencli.py -v`

Expected: FAIL because `xrag.models` and `xrag.opencli` do not exist.

- [ ] **Step 3: Implement normalized values and the subprocess boundary**

```python
# src/xrag/models.py
from dataclasses import dataclass, field


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
```

```python
# src/xrag/opencli.py
import subprocess
from collections.abc import Callable

import yaml

from .models import Post


class OpenCLIError(RuntimeError):
    pass


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_search_yaml(payload: str, keyword: str) -> list[Post]:
    rows = yaml.safe_load(payload) or []
    if not isinstance(rows, list):
        raise OpenCLIError("OpenCLI YAML result must be a list")
    posts = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id") or not row.get("text"):
            continue
        posts.append(Post(
            id=str(row["id"]),
            author=str(row.get("author") or "unknown"),
            bio=str(row.get("bio") or ""),
            text=str(row["text"]).strip(),
            created_at=str(row.get("created_at") or ""),
            likes=_integer(row.get("likes")),
            views=_integer(row.get("views")),
            url=str(row.get("url") or f"https://x.com/i/status/{row['id']}"),
            media_urls=tuple(str(url) for url in (row.get("media_urls") or []) if url),
            source_keywords=(keyword,),
        ))
    return posts


class OpenCLIClient:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def search(self, keyword: str, limit: int) -> list[Post]:
        args = ["opencli", "twitter", "search", keyword, "--limit", str(limit), "-f", "yaml"]
        result = self._run(args, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown OpenCLI error").strip()
            raise OpenCLIError(message)
        return parse_search_yaml(result.stdout, keyword)
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_opencli.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/models.py src/xrag/opencli.py tests/fixtures/opencli-search.yaml tests/test_opencli.py
git commit -m "feat: normalize OpenCLI X search results"
```

### Task 3: Canonical Markdown repository with deduplication

**Files:**
- Create: `src/xrag/markdown_store.py`
- Test: `tests/test_markdown_store.py`

- [ ] **Step 1: Write failing Markdown round-trip and update tests**

```python
# tests/test_markdown_store.py
from pathlib import Path

from xrag.markdown_store import MarkdownStore
from xrag.models import Post


def make_post(**changes):
    values = dict(
        id="123", author="alice", bio="bio", text="第一段\n\n第二段", created_at="2026-08-04",
        likes=5, views=100, url="https://x.com/i/status/123",
        media_urls=("https://pbs.twimg.com/a.jpg",), source_keywords=("AI",),
    )
    values.update(changes)
    return Post(**values)


def test_upsert_writes_utf8_markdown_and_reads_it_back(tmp_path: Path):
    store = MarkdownStore(tmp_path)
    path = store.upsert(make_post())

    loaded = store.read(path)

    assert path.name == "123.md"
    assert loaded.text == "第一段\n\n第二段"
    assert loaded.media_urls == ("https://pbs.twimg.com/a.jpg",)


def test_upsert_merges_keywords_and_refreshes_metrics(tmp_path: Path):
    store = MarkdownStore(tmp_path)
    store.upsert(make_post())

    store.upsert(make_post(likes=9, views=250, source_keywords=("GPU",)))
    loaded = store.read(tmp_path / "123.md")

    assert loaded.likes == 9
    assert loaded.views == 250
    assert loaded.source_keywords == ("AI", "GPU")
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_markdown_store.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'xrag.markdown_store'`.

- [ ] **Step 3: Implement deterministic Markdown serialization and upsert**

```python
# src/xrag/markdown_store.py
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .models import Post


class MarkdownStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, post_id: str) -> Path:
        return self.directory / f"{post_id}.md"

    def upsert(self, post: Post) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(post.id)
        collected_at = datetime.now(timezone.utc).isoformat()
        keywords = post.source_keywords
        if path.exists():
            old = self.read(path)
            keywords = tuple(dict.fromkeys((*old.source_keywords, *post.source_keywords)))
        metadata = {
            "id": post.id, "author": post.author, "author_bio": post.bio,
            "created_at": post.created_at, "collected_at": collected_at,
            "updated_at": collected_at, "url": post.url, "likes": post.likes,
            "views": post.views, "media_urls": list(post.media_urls),
            "source_keywords": list(keywords), "source_type": post.source_type,
        }
        front = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
        path.write_text(f"---\n{front}\n---\n\n{post.text.strip()}\n", encoding="utf-8")
        return path

    def read(self, path: Path) -> Post:
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            raise ValueError(f"invalid Markdown front matter: {path}")
        raw_front, text = content[4:].split("\n---\n", 1)
        metadata = yaml.safe_load(raw_front)
        return Post(
            id=str(metadata["id"]), author=str(metadata["author"]),
            bio=str(metadata.get("author_bio") or ""), text=text.strip(),
            created_at=str(metadata.get("created_at") or ""),
            likes=int(metadata.get("likes") or 0), views=int(metadata.get("views") or 0),
            url=str(metadata["url"]),
            media_urls=tuple(metadata.get("media_urls") or ()),
            source_keywords=tuple(metadata.get("source_keywords") or ()),
            source_type=str(metadata.get("source_type") or "import"),
        )

    def iter_posts(self):
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.md")):
            yield path, self.read(path)
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_markdown_store.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/markdown_store.py tests/test_markdown_store.py
git commit -m "feat: persist canonical X posts as Markdown"
```

### Task 4: Import YAML, JSON, and Markdown

**Files:**
- Create: `src/xrag/importers.py`
- Test: `tests/test_importers.py`

- [ ] **Step 1: Write failing adapter tests**

```python
# tests/test_importers.py
import json
from pathlib import Path

from xrag.importers import load_posts


def test_load_posts_accepts_yaml_and_json_lists(tmp_path: Path):
    yaml_path = tmp_path / "posts.yaml"
    yaml_path.write_text("- {id: '1', author: a, text: 中文, url: 'https://x.com/i/status/1'}", encoding="utf-8")
    json_path = tmp_path / "posts.json"
    json_path.write_text(json.dumps([{"id": "2", "author": "b", "text": "AI", "url": "https://x.com/i/status/2"}]), encoding="utf-8")

    assert [post.id for post in load_posts(yaml_path)] == ["1"]
    assert [post.id for post in load_posts(json_path)] == ["2"]


def test_load_posts_accepts_canonical_markdown(tmp_path: Path):
    path = tmp_path / "3.md"
    path.write_text("---\nid: '3'\nauthor: c\nurl: https://x.com/i/status/3\n---\n\n原文\n", encoding="utf-8")

    post = load_posts(path)[0]

    assert post.id == "3"
    assert post.text == "原文"
    assert post.source_type == "import"
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_importers.py -v`

Expected: FAIL because `xrag.importers` does not exist.

- [ ] **Step 3: Implement extension-specific normalization**

```python
# src/xrag/importers.py
import json
from pathlib import Path

import yaml

from .models import Post


def _post(row: dict, fallback_id: str = "") -> Post:
    post_id = str(row.get("id") or fallback_id)
    text = str(row.get("text") or "").strip()
    if not post_id or not text:
        raise ValueError("imported post requires id and text")
    return Post(
        id=post_id, author=str(row.get("author") or "unknown"), text=text,
        bio=str(row.get("bio") or row.get("author_bio") or ""),
        created_at=str(row.get("created_at") or ""), url=str(row.get("url") or f"https://x.com/i/status/{post_id}"),
        likes=int(row.get("likes") or 0), views=int(row.get("views") or 0),
        media_urls=tuple(row.get("media_urls") or ()),
        source_keywords=tuple(row.get("source_keywords") or ()), source_type="import",
    )


def load_posts(path: Path) -> list[Post]:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    elif suffix == ".json":
        rows = json.loads(path.read_text(encoding="utf-8"))
    elif suffix == ".md":
        content = path.read_text(encoding="utf-8")
        if not content.startswith("---\n") or "\n---\n" not in content[4:]:
            raise ValueError(f"Markdown requires YAML front matter: {path}")
        front, text = content[4:].split("\n---\n", 1)
        row = yaml.safe_load(front) or {}
        row["text"] = text.strip()
        rows = [row]
    else:
        raise ValueError(f"unsupported import extension: {suffix}")
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise ValueError(f"import root must be a list or object: {path}")
    return [_post(row, path.stem) for row in rows if isinstance(row, dict)]
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_importers.py -v`

Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/importers.py tests/test_importers.py
git commit -m "feat: import YAML JSON and Markdown posts"
```

### Task 5: Chinese-friendly chunking and Chroma indexing

**Files:**
- Create: `src/xrag/chunking.py`
- Create: `src/xrag/vector_store.py`
- Test: `tests/test_chunking.py`
- Test: `tests/test_vector_store.py`

- [ ] **Step 1: Write failing deterministic chunk tests**

```python
# tests/test_chunking.py
from xrag.chunking import chunk_text


def test_chunk_text_keeps_short_paragraphs_and_splits_long_ones():
    text = "第一段。\n\n" + "中文内容。" * 80

    chunks = chunk_text(text, max_chars=120, overlap=20)

    assert chunks[0] == "第一段。"
    assert all(0 < len(chunk) <= 120 for chunk in chunks)
    assert len(chunks) > 2
```

```python
# tests/test_vector_store.py
from pathlib import Path

from xrag.models import Post
from xrag.vector_store import VectorStore


class FakeCollection:
    def __init__(self):
        self.upserts = []
        self.deleted = []
    def upsert(self, **kwargs):
        self.upserts.append(kwargs)
    def delete(self, **kwargs):
        self.deleted.append(kwargs)
    def query(self, **kwargs):
        return {"ids": [["123:0"]], "documents": [["人工智能原文"]], "metadatas": [[{
            "post_id": "123", "author": "alice", "created_at": "2026", "url": "https://x.com/i/status/123",
            "markdown_path": "data/markdown/123.md",
        }]], "distances": [[0.2]]}
    def count(self):
        return len(self.upserts)


def test_index_post_uses_stable_chunk_ids_and_search_maps_distance():
    collection = FakeCollection()
    store = VectorStore(collection)
    post = Post(id="123", author="alice", text="人工智能原文", created_at="2026", url="https://x.com/i/status/123")

    store.index_post(post, Path("data/markdown/123.md"))
    hits = store.search("人工智能", 5)

    assert collection.upserts[0]["ids"] == ["123:0"]
    assert collection.deleted[0] == {"where": {"post_id": "123"}}
    assert hits[0].score == 0.8
    assert hits[0].url == "https://x.com/i/status/123"
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_chunking.py tests/test_vector_store.py -v`

Expected: FAIL because chunking and vector modules do not exist.

- [ ] **Step 3: Implement chunking and an injectable vector adapter**

```python
# src/xrag/chunking.py
def chunk_text(text: str, max_chars: int = 500, overlap: int = 80) -> list[str]:
    paragraphs = [part.strip() for part in text.replace("\r\n", "\n").split("\n\n") if part.strip()]
    chunks = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            end = min(start + max_chars, len(paragraph))
            chunks.append(paragraph[start:end].strip())
            if end == len(paragraph):
                break
            start = end - overlap
    return chunks
```

```python
# src/xrag/vector_store.py
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

from .chunking import chunk_text
from .models import Post, SearchHit


class VectorStore:
    def __init__(self, collection):
        self.collection = collection

    @classmethod
    def persistent(cls, path: Path, model_name: str):
        path.mkdir(parents=True, exist_ok=True)
        embedding = SentenceTransformerEmbeddingFunction(
            model_name=model_name, device="cpu", normalize_embeddings=True,
        )
        client = chromadb.PersistentClient(path=str(path))
        collection = client.get_or_create_collection(
            name="x_posts", embedding_function=embedding, metadata={"hnsw:space": "cosine"},
        )
        return cls(collection)

    def index_post(self, post: Post, markdown_path: Path) -> int:
        chunks = chunk_text(post.text)
        if not chunks:
            return 0
        self.collection.delete(where={"post_id": post.id})
        metadata = {
            "post_id": post.id, "author": post.author, "created_at": post.created_at,
            "url": post.url, "markdown_path": str(markdown_path),
        }
        self.collection.upsert(
            ids=[f"{post.id}:{index}" for index in range(len(chunks))], documents=chunks,
            metadatas=[metadata for _ in chunks],
        )
        return len(chunks)

    def search(self, query: str, top: int) -> list[SearchHit]:
        result = self.collection.query(query_texts=[query], n_results=top)
        hits = []
        for text, metadata, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True,
        ):
            hits.append(SearchHit(
                post_id=metadata["post_id"], text=text, author=metadata["author"],
                created_at=metadata["created_at"], url=metadata["url"],
                score=round(max(0.0, 1.0 - float(distance)), 4), markdown_path=metadata["markdown_path"],
            ))
        return hits

    def count(self) -> int:
        return self.collection.count()

    def clear(self) -> None:
        result = self.collection.get(include=[])
        ids = result.get("ids", [])
        if ids:
            self.collection.delete(ids=ids)
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/pytest tests/test_chunking.py tests/test_vector_store.py -v`

Expected: `2 passed` without downloading the embedding model because the vector test injects a fake collection.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/chunking.py src/xrag/vector_store.py tests/test_chunking.py tests/test_vector_store.py
git commit -m "feat: chunk Chinese text and index posts in Chroma"
```

### Task 6: Orchestration, locking, and status

**Files:**
- Create: `src/xrag/locking.py`
- Create: `src/xrag/service.py`
- Test: `tests/test_service.py`

- [ ] **Step 1: Write failing collection, import, rebuild, and status tests**

```python
# tests/test_service.py
from pathlib import Path

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.models import Post
from xrag.service import XragService


class FakeOpenCLI:
    def search(self, keyword, limit):
        return [Post(id="1", author="a", text="AI 原文", created_at="2026", url="https://x.com/i/status/1", source_keywords=(keyword,))]


class FakeVectors:
    def __init__(self): self.indexed = []
    def index_post(self, post, path): self.indexed.append((post.id, path)); return 1
    def search(self, query, top): return []
    def count(self): return len(self.indexed)
    def clear(self): self.indexed.clear()


def config(root):
    return AppConfig(root, True, "10:00", "Asia/Singapore", 50, 0, ("AI",), "model")


def test_collect_persists_then_indexes(tmp_path: Path):
    vectors = FakeVectors()
    service = XragService(config(tmp_path), FakeOpenCLI(), MarkdownStore(tmp_path / "data/markdown"), vectors)

    result = service.collect("AI", 50)

    assert result == {"found": 1, "stored": 1, "chunks": 1, "errors": 0}
    assert (tmp_path / "data/markdown/1.md").exists()
    assert vectors.indexed[0][0] == "1"


def test_status_counts_markdown_and_chunks(tmp_path: Path):
    vectors = FakeVectors()
    store = MarkdownStore(tmp_path / "data/markdown")
    store.upsert(Post(id="1", author="a", text="AI", created_at="", url="https://x.com/i/status/1"))
    service = XragService(config(tmp_path), FakeOpenCLI(), store, vectors)

    assert service.status()["documents"] == 1
    assert service.status()["chunks"] == 0
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_service.py -v`

Expected: FAIL because `xrag.service` does not exist.

- [ ] **Step 3: Implement the write lock and use cases**

```python
# src/xrag/locking.py
from contextlib import contextmanager
from pathlib import Path

import portalocker


@contextmanager
def writer_lock(root: Path, timeout: int = 1):
    root.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(root / ".xrag.lock", mode="a", timeout=timeout):
        yield
```

```python
# src/xrag/service.py
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from .importers import load_posts
from .locking import writer_lock


class XragService:
    def __init__(self, config, opencli, markdown, vectors):
        self.config = config
        self.opencli = opencli
        self.markdown = markdown
        self.vectors = vectors

    def collect(self, keyword: str, limit: int | None = None):
        posts = self.opencli.search(keyword, limit or self.config.limit_per_keyword)
        stored = chunks = errors = 0
        with writer_lock(self.config.root):
            for post in posts:
                try:
                    path = self.markdown.upsert(post)
                    chunks += self.vectors.index_post(post, path)
                    stored += 1
                except (ValueError, OSError):
                    errors += 1
        self._write_last_run("collect", {"keyword": keyword, "stored": stored, "errors": errors})
        return {"found": len(posts), "stored": stored, "chunks": chunks, "errors": errors}

    def collect_all(self):
        results = []
        for index, keyword in enumerate(self.config.keywords):
            results.append((keyword, self.collect(keyword)))
            if index + 1 < len(self.config.keywords):
                time.sleep(self.config.delay_seconds)
        return results

    def import_path(self, source: Path):
        files = [source] if source.is_file() else sorted(path for path in source.rglob("*") if path.suffix.lower() in {".yaml", ".yml", ".json", ".md"})
        imported = errors = 0
        with writer_lock(self.config.root):
            for path in files:
                try:
                    for post in load_posts(path):
                        saved = self.markdown.upsert(post)
                        self.vectors.index_post(post, saved)
                        imported += 1
                except (ValueError, OSError, json.JSONDecodeError):
                    errors += 1
        return {"files": len(files), "imported": imported, "errors": errors}

    def search(self, query: str, top: int):
        return self.vectors.search(query, top)

    def rebuild(self):
        chunks = 0
        with writer_lock(self.config.root):
            self.vectors.clear()
            for path, post in self.markdown.iter_posts():
                chunks += self.vectors.index_post(post, path)
        return {"documents": sum(1 for _ in self.markdown.iter_posts()), "chunks": chunks}

    def status(self):
        documents = sum(1 for _ in self.markdown.iter_posts())
        last_run = self.config.log_dir / "last-run.json"
        return {"documents": documents, "chunks": self.vectors.count(), "keywords": len(self.config.keywords), "last_run": json.loads(last_run.read_text(encoding="utf-8")) if last_run.exists() else None}

    def _write_last_run(self, operation, result):
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        payload = {"operation": operation, "at": datetime.now(timezone.utc).isoformat(), **result}
        (self.config.log_dir / "last-run.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 4: Verify GREEN and run the full unit suite**

Run: `.venv/bin/pytest -v`

Expected: all tests through Task 6 pass.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/locking.py src/xrag/service.py tests/test_service.py
git commit -m "feat: orchestrate collection imports and indexing"
```

### Task 7: User-facing CLI

**Files:**
- Create: `src/xrag/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI output tests**

```python
# tests/test_cli.py
from typer.testing import CliRunner

import xrag.cli as cli
from xrag.models import SearchHit


runner = CliRunner()


class FakeService:
    def collect(self, keyword, limit): return {"found": 2, "stored": 2, "chunks": 3, "errors": 0}
    def search(self, query, top):
        return [SearchHit("1", "AI 原文", "alice", "2026", "https://x.com/i/status/1", 0.91, "data/markdown/1.md")]
    def status(self): return {"documents": 1, "chunks": 2, "keywords": 1, "last_run": None}


def test_collect_prints_machine_readable_summary(monkeypatch):
    monkeypatch.setattr(cli, "build_service", lambda root: FakeService())
    result = runner.invoke(cli.app, ["--root", ".", "collect", "AI", "--limit", "20"])
    assert result.exit_code == 0
    assert "stored=2" in result.stdout


def test_search_prints_score_source_and_original_url(monkeypatch):
    monkeypatch.setattr(cli, "build_service", lambda root: FakeService())
    result = runner.invoke(cli.app, ["--root", ".", "search", "AI 趋势", "--top", "5"])
    assert result.exit_code == 0
    assert "0.9100" in result.stdout
    assert "https://x.com/i/status/1" in result.stdout
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_cli.py -v`

Expected: FAIL because `xrag.cli` does not exist.

- [ ] **Step 3: Implement service construction and commands**

```python
# src/xrag/cli.py
import json
from pathlib import Path

import typer

from .config import load_config
from .markdown_store import MarkdownStore
from .opencli import OpenCLIClient, OpenCLIError
from .service import XragService
from .vector_store import VectorStore


app = typer.Typer(no_args_is_help=True)


def build_service(root: Path):
    config = load_config(root)
    markdown = MarkdownStore(config.markdown_dir)
    vectors = VectorStore.persistent(config.chroma_dir, config.embedding_model)
    return XragService(config, OpenCLIClient(), markdown, vectors)


def _service(ctx: typer.Context):
    return build_service(ctx.obj["root"])


@app.callback()
def main(ctx: typer.Context, root: Path = typer.Option(Path.cwd(), exists=True, file_okay=False)):
    ctx.obj = {"root": root.resolve()}


@app.command()
def collect(ctx: typer.Context, keyword: str | None = typer.Argument(None), limit: int | None = typer.Option(None), all_keywords: bool = typer.Option(False, "--all")):
    service = _service(ctx)
    try:
        if all_keywords:
            for name, result in service.collect_all():
                typer.echo(f"keyword={name} " + " ".join(f"{key}={value}" for key, value in result.items()))
        elif keyword:
            result = service.collect(keyword, limit)
            typer.echo(" ".join(f"{key}={value}" for key, value in result.items()))
        else:
            raise typer.BadParameter("provide KEYWORD or --all")
    except OpenCLIError as exc:
        typer.echo(f"OpenCLI error: {exc}", err=True)
        raise typer.Exit(2)


@app.command("import")
def import_files(ctx: typer.Context, source: Path):
    typer.echo(json.dumps(_service(ctx).import_path(source), ensure_ascii=False))


@app.command()
def search(ctx: typer.Context, query: str, top: int = typer.Option(10, min=1, max=100)):
    for index, hit in enumerate(_service(ctx).search(query, top), 1):
        typer.echo(f"[{index}] score={hit.score:.4f} @{hit.author} {hit.created_at}\n{hit.text}\n{hit.url}\nmarkdown: {hit.markdown_path}\n")


@app.command()
def status(ctx: typer.Context):
    typer.echo(json.dumps(_service(ctx).status(), ensure_ascii=False, indent=2))


@app.command()
def rebuild(ctx: typer.Context):
    typer.echo(json.dumps(_service(ctx).rebuild(), ensure_ascii=False))
```

- [ ] **Step 4: Verify GREEN and verify the installed console entry point**

Run:

```bash
.venv/bin/pytest tests/test_cli.py -v
.venv/bin/xrag --help
```

Expected: CLI tests pass and help lists `collect`, `import`, `search`, `status`, and `rebuild`.

- [ ] **Step 5: Commit**

```bash
git add src/xrag/cli.py tests/test_cli.py
git commit -m "feat: expose xrag command line interface"
```

### Task 8: Idempotent daily 10:00 scheduler

**Files:**
- Create: `scripts/run-daily.sh`
- Create: `scripts/install-schedule.ps1`
- Test: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Write failing static scheduler tests**

```python
# tests/test_scheduler_scripts.py
from pathlib import Path


def test_daily_runner_uses_project_venv_and_all_keywords():
    text = Path("scripts/run-daily.sh").read_text(encoding="utf-8")
    assert '"$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" collect --all' in text


def test_installer_creates_one_daily_task_at_configured_time():
    text = Path("scripts/install-schedule.ps1").read_text(encoding="utf-8")
    assert '$TaskName = "X-RAG Daily Collection"' in text
    assert "New-ScheduledTaskTrigger -Daily -At $ScheduleTime" in text
    assert "Register-ScheduledTask -Force" in text
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/pytest tests/test_scheduler_scripts.py -v`

Expected: FAIL with `FileNotFoundError` for the scheduler scripts.

- [ ] **Step 3: Implement the WSL runner and PowerShell installer**

```bash
#!/usr/bin/env bash
# scripts/run-daily.sh
set -euo pipefail
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$PROJECT_ROOT/logs"
"$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" collect --all >> "$PROJECT_ROOT/logs/scheduler.log" 2>&1
```

```powershell
# scripts/install-schedule.ps1
param(
    [string]$Distribution = "Ubuntu",
    [string]$ScheduleTime = "10:00"
)

$ErrorActionPreference = "Stop"
$TaskName = "X-RAG Daily Collection"
$ProjectWindows = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ProjectWsl = (wsl.exe -d $Distribution -- wslpath -a $ProjectWindows).Trim()
if (-not $ProjectWsl) { throw "Could not translate the project path to WSL." }

$RunnerWsl = "$ProjectWsl/scripts/run-daily.sh"
$Action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distribution -- bash `"$RunnerWsl`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $ScheduleTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Collect configured X keywords into the local Markdown RAG database." -Force | Out-Null
Write-Output "Installed '$TaskName' at $ScheduleTime every day."
```

- [ ] **Step 4: Verify GREEN and install/query the task**

Run:

```bash
.venv/bin/pytest tests/test_scheduler_scripts.py -v
```

Then from PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -ScheduleTime 10:00
Get-ScheduledTask -TaskName "X-RAG Daily Collection" | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName "X-RAG Daily Collection" | Select-Object NextRunTime
```

Expected: tests pass; exactly one task is present; `NextRunTime` is the next local 10:00 occurrence.

- [ ] **Step 5: Commit**

```bash
git add scripts/run-daily.sh scripts/install-schedule.ps1 tests/test_scheduler_scripts.py
git commit -m "feat: schedule daily X collection at 10am"
```

### Task 9: Chinese operating guide and offline fixture integration

**Files:**
- Create: `README.md`
- Create: `tests/test_offline_flow.py`

- [ ] **Step 1: Write the failing offline end-to-end test**

```python
# tests/test_offline_flow.py
from pathlib import Path

from xrag.markdown_store import MarkdownStore
from xrag.opencli import parse_search_yaml


def test_fixture_search_result_becomes_rebuildable_markdown(tmp_path: Path):
    payload = Path("tests/fixtures/opencli-search.yaml").read_text(encoding="utf-8")
    store = MarkdownStore(tmp_path / "data/markdown")

    for post in parse_search_yaml(payload, "DDR5"):
        store.upsert(post)

    posts = list(store.iter_posts())
    assert len(posts) == 1
    assert posts[0][1].source_keywords == ("DDR5",)
    assert posts[0][1].url == "https://x.com/i/status/2084640002085130466"
```

- [ ] **Step 2: Run the offline flow before writing documentation**

Run: `.venv/bin/pytest tests/test_offline_flow.py -v`

Expected: PASS using completed components. This is a regression/integration test, so verify its validity once by temporarily changing the expected ID to an incorrect value, observing FAIL, and restoring the correct ID before continuing.

- [ ] **Step 3: Write the Chinese README with exact commands**

```markdown
# X RAG 本地资料库

资料库通过 OpenCLI 收集 X 帖子，以 Markdown 为原始档案，用本地中文嵌入模型和 ChromaDB 完成语义检索。数据不上传到生成式 AI 服务。

## 安装

```bash
cd "/mnt/c/Users/1/Documents/X工作流"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/xrag --help
```

首次运行查询或入库时会下载 `paraphrase-multilingual-MiniLM-L12-v2` 模型。

## 使用

```bash
.venv/bin/xrag collect "AI 视频" --limit 50
.venv/bin/xrag collect --all
.venv/bin/xrag import data/imports
.venv/bin/xrag search "AI 视频有什么新趋势？" --top 10
.venv/bin/xrag status
.venv/bin/xrag rebuild
```

修改 `config/keywords.yaml` 管理关键词。修改定时时间后，在 Windows PowerShell 重新执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -ScheduleTime 10:00
```

Markdown 保存在 `data/markdown/`，待导入文件可放入 `data/imports/`，定时日志保存在 `logs/scheduler.log`。
```

- [ ] **Step 4: Run all tests and inspect package metadata**

Run:

```bash
.venv/bin/pytest -v
.venv/bin/python -m pip check
.venv/bin/xrag --help
```

Expected: all tests pass, `pip check` reports no broken requirements, and all five commands appear in help.

- [ ] **Step 5: Commit**

```bash
git add README.md tests/test_offline_flow.py
git commit -m "docs: add Chinese xrag operating guide"
```

### Task 10: Live X-to-Markdown-to-search acceptance

**Files:**
- Modify: `config/keywords.yaml` only if the user supplies a different production keyword.
- Runtime output: `data/markdown/*.md`, `data/chroma/`, `logs/last-run.json` (all ignored by Git).

- [ ] **Step 1: Confirm the existing OpenCLI bridge immediately before acceptance**

Run:

```bash
opencli doctor
```

Expected: daemon running, extension connected, and connectivity connected.

- [ ] **Step 2: Run a small real collection**

Run:

```bash
.venv/bin/xrag --root . collect "人工智能" --limit 5
```

Expected: exit code 0, `found` is at least 1, `stored` equals the number of valid returned posts, `errors=0`, and Markdown files appear under `data/markdown/`.

- [ ] **Step 3: Verify the generated Markdown contains traceable metadata and no credentials**

Run:

```bash
rg -n -m 20 '^(id|author|url|source_keywords):' data/markdown
rg -n 'auth_token|ct0|TWITTER_AUTH_TOKEN|TWITTER_CT0' data/markdown logs || true
```

Expected: the first command prints metadata; the credential scan prints nothing.

- [ ] **Step 4: Run a real Chinese semantic query and rebuild check**

Run:

```bash
.venv/bin/xrag --root . search "人工智能行业最近有什么趋势？" --top 5
.venv/bin/xrag --root . rebuild
.venv/bin/xrag --root . search "人工智能行业最近有什么趋势？" --top 5
```

Expected: both searches return at least one result with a Markdown path and `https://x.com/` URL; rebuild reports at least one document and one chunk.

- [ ] **Step 5: Verify scheduled task and final repository state**

Run from PowerShell:

```powershell
Get-ScheduledTask -TaskName "X-RAG Daily Collection" | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName "X-RAG Daily Collection" | Select-Object NextRunTime
git status --short
```

Expected: one scheduled task exists, its next run is the next local 10:00 occurrence, and Git shows no uncommitted tracked changes.

## Plan self-review record

- Spec coverage: canonical Markdown, OpenCLI collection, YAML/JSON/Markdown imports, local semantic indexing, retrieval-only output, deduplication, metric refresh, rebuild, locking, logs, credential exclusion, 10:00 scheduling, Chinese documentation, and live acceptance each map to a task above.
- Placeholder scan: the plan contains no unresolved placeholder markers or unspecified error-handling steps.
- Type consistency: `Post`, `SearchHit`, `AppConfig`, `MarkdownStore`, `VectorStore`, and `XragService` names and fields are consistent across tasks.
- Scope: the plan produces one testable local CLI and one scheduler integration; no web UI or generative model is included.
