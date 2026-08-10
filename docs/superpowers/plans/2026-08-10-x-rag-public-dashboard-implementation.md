# X-RAG Public Hotspot Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and publish a white, pastel-card GitHub Pages dashboard that ranks the existing canonical X-RAG Markdown archive into safe daily AI and Web3 hotspots.

**Architecture:** Add a deterministic scoring module and a read-only static-site builder to the existing Python package. The builder emits an allowlisted JSON snapshot plus content-addressed media, while a separate publisher copies only generated public files into a dedicated `gh-pages` worktree and pushes them; the current 10:00 scheduled task calls the same validated update pipeline.

**Tech Stack:** Python 3.11+, dataclasses, `zoneinfo`, Typer, stdlib JSON/hash/path/subprocess utilities, semantic HTML, CSS Grid, browser-native JavaScript modules, Node 22 built-in tests, pytest, GitHub Pages, Windows Task Scheduler, WSL Ubuntu.

---

Run Python and pytest commands from the repository root in WSL with the existing `.venv`. Run PowerShell commands from `C:\Users\1\Documents\X工作流\.worktrees\x-rag` unless a step explicitly targets the normal non-worktree installation.

## File map

- Create `src/xrag/dashboard_scoring.py`: topic assignment, candidate windows, deduplication, normalized scoring, tie-breaking, and per-author diversity.
- Create `src/xrag/dashboard_export.py`: public snapshot model, allowlisted serialization, safe media export, secret/path scan, and atomic site writes.
- Create `src/xrag/dashboard_publish.py`: non-destructive dedicated `gh-pages` worktree setup, explicit-path staging, commit, and push.
- Create `dashboard/index.html`: accessible data-cockpit page shell and templates.
- Create `dashboard/assets/styles.css`: white-first responsive design and pastel topic/card tokens.
- Create `dashboard/assets/app.js`: snapshot loading, rendering, filtering, details, refresh, stale-state, and safe text handling.
- Create `dashboard/tests/app.test.mjs`: dependency-free Node tests for front-end pure functions.
- Create `tests/test_dashboard_scoring.py`: deterministic ranking coverage.
- Create `tests/test_dashboard_export.py`: public schema, media, atomicity, and secret/path blocking coverage.
- Create `tests/test_dashboard_publish.py`: isolated Git command orchestration and path-safety coverage.
- Modify `src/xrag/config.py`: add generated-site and Pages-worktree paths.
- Modify `src/xrag/cli.py`: add `dashboard build`, `dashboard publish`, and `dashboard update` commands without eagerly opening Chroma.
- Modify `tests/test_config.py` and `tests/test_cli.py`: path and command behavior.
- Modify `scripts/run-daily.sh`: run collect-build-publish as one fail-fast command.
- Modify `tests/test_scheduler_scripts.py`: assert the new daily entry point and preserve scheduler safety.
- Modify `.gitignore`: ignore local generated site output while retaining source assets.
- Modify `README.md`: local preview, manual update, publication, Pages URL, failure behavior, and credential safety.

## Task 1: Add dashboard paths and deterministic scoring

**Files:**
- Create: `src/xrag/dashboard_scoring.py`
- Create: `tests/test_dashboard_scoring.py`
- Modify: `src/xrag/config.py`
- Modify: `tests/test_config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing configuration-path tests**

Add these assertions to `test_load_config_reads_valid_configuration` in `tests/test_config.py`:

```python
assert config.dashboard_dir == tmp_path / "data" / "dashboard-site"
assert config.pages_worktree == tmp_path / ".worktrees" / "x-rag-pages"
assert config.dashboard_source_dir == tmp_path / "dashboard"
```

- [ ] **Step 2: Write failing scoring tests**

Create `tests/test_dashboard_scoring.py`:

```python
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from xrag.dashboard_scoring import TOPICS, RankedPost, rank_posts
from xrag.models import LocalMedia, Post


NOW = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)  # 12:00 Singapore
KEYWORDS = tuple(f"query-{index}" for index in range(4))


def post(
    post_id: str,
    *,
    author: str = "author",
    created_at: str = "2026-08-10T03:00:00Z",
    likes: int = 10,
    views: int = 100,
    keyword: str = "query-0",
    text: str = "complete text",
    url: str | None = None,
    media: bool = False,
) -> Post:
    local_media = (
        LocalMedia(
            owner="post",
            kind="image",
            source_url="https://pbs.twimg.com/media/example.jpg",
            relative_path=f"../media/{post_id}/image-01.jpg",
            content_type="image/jpeg",
        ),
    ) if media else ()
    return Post(
        id=post_id,
        author=author,
        text=text,
        created_at=created_at,
        url=url or f"https://x.com/{author}/status/{post_id}",
        likes=likes,
        views=views,
        local_media=local_media,
        source_keywords=(keyword,),
    )


def test_topics_match_the_four_approved_groups() -> None:
    assert [(topic.id, topic.family) for topic in TOPICS] == [
        ("ai-agents-security", "AI"),
        ("world-models-embodied-ai", "AI"),
        ("rwa-stablecoin-payments", "Web3"),
        ("prediction-markets-regulation", "Web3"),
    ]


def test_multi_topic_post_uses_text_match_strength_then_configured_priority() -> None:
    queries = (
        '"Agent Security" OR "Autonomous AI Agents"',
        '"World Models" OR "Embodied AI"',
        'RWA OR "Stablecoin Payments"',
        '"Prediction Markets" OR MiCA',
    )
    item = post("multi", text="World Models and Embodied AI are converging", keyword=queries[0])
    item = replace(item, source_keywords=(queries[0], queries[1]))

    ranked = rank_posts([item], now=NOW, timezone_name="Asia/Singapore", configured_keywords=queries, minimum_today=1)

    assert ranked[0].topic.id == "world-models-embodied-ai"


def test_today_window_scores_engagement_freshness_topic_and_completeness() -> None:
    ranked = rank_posts(
        [
            post("1", views=1000, likes=100, media=True),
            post("2", views=200, likes=20),
            post("3", views=100, likes=5, keyword="query-1"),
            post("4", views=90, likes=4, keyword="query-2"),
            post("5", views=80, likes=3, keyword="query-3"),
            post("6", views=70, likes=2),
        ],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=KEYWORDS,
    )

    assert [item.post.id for item in ranked][:2] == ["1", "2"]
    assert ranked[0].topic.id == "ai-agents-security"
    assert ranked[0].score == pytest.approx(
        0.40 * ranked[0].engagement
        + 0.30 * ranked[0].freshness
        + 0.20 * ranked[0].topic_frequency
        + 0.10 * ranked[0].completeness
    )
    assert all(0.0 <= item.score <= 1.0 for item in ranked)
    assert not any(item.fallback for item in ranked)


def test_fewer_than_six_today_expands_to_48_hours_and_labels_old_posts() -> None:
    ranked = rank_posts(
        [
            post("today"),
            post("recent", created_at="2026-08-09T05:00:00Z"),
            post("too-old", created_at="2026-08-07T03:59:59Z"),
        ],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=KEYWORDS,
    )

    assert {item.post.id for item in ranked} == {"today", "recent"}
    assert next(item for item in ranked if item.post.id == "recent").fallback is True
    assert next(item for item in ranked if item.post.id == "today").fallback is False


def test_deduplication_prefers_more_complete_record_by_id_then_url() -> None:
    ranked = rank_posts(
        [
            post("same", text="", views=1),
            post("same", text="complete", views=2, media=True),
            post("other", url="https://x.com/a/status/shared", text=""),
            post("replacement", url="https://x.com/a/status/shared", media=True),
        ],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=KEYWORDS,
        minimum_today=1,
    )

    by_id = {item.post.id: item.post for item in ranked}
    assert by_id["same"].text == "complete"
    assert "replacement" in by_id
    assert "other" not in by_id


def test_author_cap_topic_priority_invalid_dates_and_stable_ties() -> None:
    posts = [post(str(index), author="same") for index in range(5)]
    posts.extend(
        [
            post("a", author="a", keyword="query-0"),
            post("b", author="b", keyword="query-1"),
            post("invalid", author="c", created_at="not-a-date"),
            post("unknown", author="d", keyword="not-configured"),
        ]
    )

    ranked = rank_posts(
        posts,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=KEYWORDS,
        minimum_today=1,
        max_per_author=3,
    )

    assert sum(item.post.author == "same" for item in ranked) == 3
    assert {item.post.id for item in ranked}.isdisjoint({"invalid", "unknown"})
    tied = [item.post.id for item in ranked if item.post.id in {"a", "b"}]
    assert tied == ["a", "b"]


def test_naive_now_or_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_posts([], now=datetime(2026, 8, 10), timezone_name="Asia/Singapore", configured_keywords=KEYWORDS)
    with pytest.raises(ValueError, match="timezone"):
        rank_posts([], now=NOW, timezone_name="Mars/Olympus", configured_keywords=KEYWORDS)
```

- [ ] **Step 3: Run the new tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_dashboard_scoring.py -v
```

Expected: collection fails because the dashboard config properties and `xrag.dashboard_scoring` do not exist.

- [ ] **Step 4: Add the three focused configuration paths**

Add to `AppConfig` in `src/xrag/config.py`:

```python
@property
def dashboard_dir(self) -> Path:
    return self.root / "data" / "dashboard-site"

@property
def dashboard_source_dir(self) -> Path:
    return self.root / "dashboard"

@property
def pages_worktree(self) -> Path:
    return self.root / ".worktrees" / "x-rag-pages"
```

Append this exact ignore rule to `.gitignore`:

```gitignore
data/dashboard-site/
```

- [ ] **Step 5: Implement the scoring module**

Create `src/xrag/dashboard_scoring.py` with these public types and functions:

```python
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
import re
from typing import Iterable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Post


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    family: Literal["AI", "Web3"]


@dataclass(frozen=True)
class RankedPost:
    post: Post
    topic: Topic
    score: float
    engagement: float
    freshness: float
    topic_frequency: float
    completeness: float
    fallback: bool


TOPICS = (
    Topic("ai-agents-security", "AI Agents 与 Agent Security", "AI"),
    Topic("world-models-embodied-ai", "World Models 与 Embodied AI", "AI"),
    Topic("rwa-stablecoin-payments", "RWA 与 Stablecoin Payments", "Web3"),
    Topic("prediction-markets-regulation", "Prediction Markets 与 Crypto Regulation", "Web3"),
)


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _query_terms(query: str) -> tuple[str, ...]:
    tokens = re.findall(r'"([^"]+)"|([\w\u4e00-\u9fff-]{2,})', query)
    terms = [next(part for part in token if part).casefold() for token in tokens]
    return tuple(dict.fromkeys(term for term in terms if term not in {"or", "and"}))


def _topic(post: Post, configured_keywords: tuple[str, ...]) -> Topic | None:
    candidates = [configured_keywords.index(value) for value in post.source_keywords if value in configured_keywords]
    if not candidates:
        return None
    haystack = post.searchable_text.casefold()
    index = min(candidates, key=lambda candidate: (-sum(term in haystack for term in _query_terms(configured_keywords[candidate])), candidate))
    return TOPICS[index] if index < len(TOPICS) else None


def _raw_completeness(post: Post) -> float:
    return (
        (0.5 if post.text.strip() else 0.0)
        + (0.3 if post.local_media else 0.0)
        + (0.2 if post.author.strip() and post.url.strip() else 0.0)
    )


def _deduplicate(posts: Iterable[Post]) -> list[Post]:
    selected: dict[str, Post] = {}
    urls: dict[str, str] = {}
    for post in posts:
        url_key = post.url.strip().casefold()
        identity = post.id.casefold()
        existing_key = identity if identity in selected else urls.get(url_key, identity)
        existing = selected.get(existing_key)
        candidate_key = existing_key
        candidate_rank = (_raw_completeness(post), post.views, post.likes, len(post.text))
        existing_rank = (-1.0, -1, -1, -1) if existing is None else (
            _raw_completeness(existing), existing.views, existing.likes, len(existing.text)
        )
        if existing is None or candidate_rank > existing_rank:
            selected[candidate_key] = post
        if url_key:
            urls[url_key] = candidate_key
    return list(selected.values())


def _log_normalize(value: int, maximum: int) -> float:
    return 0.0 if maximum <= 0 else math.log1p(max(value, 0)) / math.log1p(maximum)


def rank_posts(
    posts: Iterable[Post],
    *,
    now: datetime,
    timezone_name: str,
    configured_keywords: tuple[str, ...],
    minimum_today: int = 6,
    window_hours: int = 48,
    limit: int = 12,
    max_per_author: int = 3,
) -> list[RankedPost]:
    if now.tzinfo is None:
        raise ValueError("Dashboard clock must be timezone-aware")
    try:
        local_zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"Unknown dashboard timezone: {timezone_name}") from error
    if minimum_today < 1 or window_hours < 1 or limit < 1 or max_per_author < 1:
        raise ValueError("Dashboard ranking limits must be positive")

    prepared: list[tuple[Post, datetime, Topic]] = []
    for post in _deduplicate(posts):
        published = _timestamp(post.created_at)
        topic = _topic(post, configured_keywords)
        if published is None or topic is None or published > now + timedelta(minutes=5):
            continue
        prepared.append((post, published, topic))

    today = now.astimezone(local_zone).date()
    same_day = [item for item in prepared if item[1].astimezone(local_zone).date() == today]
    use_fallback = len(same_day) < minimum_today
    cutoff = now - timedelta(hours=window_hours)
    candidates = [item for item in prepared if cutoff <= item[1] <= now] if use_fallback else same_day
    if not candidates:
        return []

    max_views = max(item[0].views for item in candidates)
    max_likes = max(item[0].likes for item in candidates)
    topic_counts = Counter(item[2].id for item in candidates)
    max_topic_count = max(topic_counts.values())
    ranked: list[RankedPost] = []
    for post, published, topic in candidates:
        view_score = _log_normalize(post.views, max_views)
        like_score = _log_normalize(post.likes, max_likes)
        if max_views > 0 and max_likes > 0:
            engagement = 0.65 * view_score + 0.35 * like_score
        elif max_views > 0:
            engagement = view_score
        elif max_likes > 0:
            engagement = like_score
        else:
            engagement = 0.0
        age_hours = max(0.0, (now - published).total_seconds() / 3600)
        freshness = max(0.0, 1.0 - age_hours / window_hours)
        frequency = topic_counts[topic.id] / max_topic_count
        completeness = _raw_completeness(post)
        score = 0.40 * engagement + 0.30 * freshness + 0.20 * frequency + 0.10 * completeness
        ranked.append(
            RankedPost(
                post=post,
                topic=topic,
                score=score,
                engagement=engagement,
                freshness=freshness,
                topic_frequency=frequency,
                completeness=completeness,
                fallback=published.astimezone(local_zone).date() != today,
            )
        )

    ranked.sort(key=lambda item: (-item.score, -_timestamp(item.post.created_at).timestamp(), item.post.id))
    result: list[RankedPost] = []
    author_counts: Counter[str] = Counter()
    for item in ranked:
        author_key = item.post.author.strip().casefold() or "unknown"
        if author_counts[author_key] >= max_per_author:
            continue
        author_counts[author_key] += 1
        result.append(item)
        if len(result) == limit:
            break
    return result
```

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_dashboard_scoring.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit the scoring slice**

```bash
git add .gitignore src/xrag/config.py src/xrag/dashboard_scoring.py tests/test_config.py tests/test_dashboard_scoring.py
git commit -m "feat: rank daily dashboard hotspots"
```

## Task 2: Build safe public snapshots and export selected media

**Files:**
- Create: `src/xrag/dashboard_export.py`
- Create: `tests/test_dashboard_export.py`

- [ ] **Step 1: Write failing exporter tests**

Create `tests/test_dashboard_export.py` with fixtures that use a real `MarkdownStore` and tiny JPEG-signature files:

```python
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from xrag.config import AppConfig
from xrag.dashboard_export import DashboardBuilder
from xrag.markdown_store import MarkdownStore
from xrag.models import LocalMedia, Post


def configuration(root: Path) -> AppConfig:
    return AppConfig(
        root=root,
        schedule_enabled=True,
        schedule_time="10:00",
        timezone="Asia/Singapore",
        limit_per_keyword=10,
        delay_seconds=0,
        keywords=("query-0", "query-1", "query-2", "query-3"),
        embedding_model="test-model",
    )


def seed(root: Path, post_id: str = "1", text: str = "热点正文") -> Path:
    media_dir = root / "data" / "media" / post_id
    media_dir.mkdir(parents=True, exist_ok=True)
    image = media_dir / "image-01.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" + b"safe-image")
    MarkdownStore(root / "data" / "markdown", clock=lambda: "2026-08-10T04:00:00Z").upsert(
        Post(
            id=post_id,
            author="alice",
            text=text,
            created_at="2026-08-10T03:00:00Z",
            url=f"https://x.com/alice/status/{post_id}",
            likes=5,
            views=100,
            source_keywords=("query-0",),
            local_media=(
                LocalMedia("post", "image", "https://pbs.twimg.com/media/a.jpg", f"../media/{post_id}/image-01.jpg", "image/jpeg"),
            ),
        )
    )
    return image


def builder(root: Path) -> DashboardBuilder:
    source = root / "dashboard"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text("<main id=app></main>", encoding="utf-8")
    (source / "assets" / "styles.css").write_text("body{background:white}", encoding="utf-8")
    (source / "assets" / "app.js").write_text("export const ok=true;", encoding="utf-8")
    return DashboardBuilder(
        configuration(root),
        MarkdownStore(root / "data" / "markdown"),
        clock=lambda: datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc),
    )


def test_build_writes_allowlisted_snapshot_assets_and_latest_last(tmp_path: Path) -> None:
    source_image = seed(tmp_path)
    before = source_image.read_bytes()

    result = builder(tmp_path).build()

    latest = json.loads((tmp_path / "data" / "dashboard-site" / "data" / "latest.json").read_text(encoding="utf-8"))
    assert result["posts"] == 1
    assert latest["version"] == 1
    assert latest["timezone"] == "Asia/Singapore"
    assert latest["summary"] == {"posts": 1, "authors": 1, "media": 1, "engagement": 105}
    assert latest["topics"][0]["top_keyword"] == "query-0"
    public = latest["posts"][0]
    assert set(public) == {
        "id", "author", "text", "created_at", "url", "likes", "views",
        "topic", "family", "keywords", "score", "fallback", "media",
    }
    assert public["media"][0]["url"].startswith("assets/media/")
    assert not Path(public["media"][0]["url"]).is_absolute()
    exported = tmp_path / "data" / "dashboard-site" / public["media"][0]["url"]
    assert exported.read_bytes() == before
    assert source_image.read_bytes() == before
    assert (tmp_path / "data" / "dashboard-site" / "index.html").exists()
    assert list((tmp_path / "data" / "dashboard-site" / "data").glob("2026-08-10T*.json"))


@pytest.mark.parametrize(
    "secret",
    [
        "auth_token=secret-value",
        "ct0: secret-value",
        r"C:\\Users\\owner\\private.jpg",
        "/mnt/c/Users/owner/private.jpg",
        "/home/owner/private.jpg",
    ],
)
def test_build_blocks_secrets_and_absolute_local_paths_without_replacing_latest(tmp_path: Path, secret: str) -> None:
    seed(tmp_path, text="safe")
    instance = builder(tmp_path)
    instance.build()
    latest_path = tmp_path / "data" / "dashboard-site" / "data" / "latest.json"
    previous = latest_path.read_bytes()
    MarkdownStore(tmp_path / "data" / "markdown").upsert(
        Post(id="2", author="bad", text=secret, created_at="2026-08-10T03:30:00Z", url="https://x.com/bad/status/2", source_keywords=("query-0",))
    )

    with pytest.raises(ValueError, match="unsafe public output"):
        instance.build()

    assert latest_path.read_bytes() == previous


def test_build_rejects_empty_candidates_and_media_escape_without_touching_latest(tmp_path: Path) -> None:
    instance = builder(tmp_path)
    with pytest.raises(ValueError, match="No valid dashboard candidates"):
        instance.build()

    seed(tmp_path)
    instance.build()
    latest_path = tmp_path / "data" / "dashboard-site" / "data" / "latest.json"
    previous = latest_path.read_bytes()
    MarkdownStore(tmp_path / "data" / "markdown").upsert(
        Post(
            id="escape",
            author="bad",
            text="bad media path",
            created_at="2026-08-10T03:30:00Z",
            url="https://x.com/bad/status/escape",
            source_keywords=("query-0",),
            local_media=(LocalMedia("post", "image", "https://pbs.twimg.com/media/b.jpg", "../../outside.jpg", "image/jpeg"),),
        )
    )
    with pytest.raises(ValueError, match="media path"):
        instance.build()
    assert latest_path.read_bytes() == previous


def test_build_rejects_non_https_or_non_x_source_links(tmp_path: Path) -> None:
    MarkdownStore(tmp_path / "data" / "markdown").upsert(
        Post(
            id="bad-link",
            author="bad",
            text="unsafe link",
            created_at="2026-08-10T03:30:00Z",
            url="javascript:alert(1)",
            source_keywords=("query-0",),
        )
    )

    with pytest.raises(ValueError, match="X URL"):
        builder(tmp_path).build()
```

- [ ] **Step 2: Run the exporter tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_dashboard_export.py -v
```

Expected: collection fails because `DashboardBuilder` does not exist.

- [ ] **Step 3: Implement public snapshot building**

Create `src/xrag/dashboard_export.py`. Use `rank_posts` as the only ranking source and implement these exact boundaries:

```python
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
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .config import AppConfig
from .dashboard_scoring import RankedPost, TOPICS, rank_posts
from .markdown_store import MarkdownStore


_UNSAFE = re.compile(
    r"(?i)(?:\bauth[_-]?token\b|\bct0\b|\bauthorization\b|[A-Z]:\\|/mnt/[a-z]/|/home/)",
)
_IMAGE_SIGNATURES = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def assert_public_content(content: str) -> None:
    if _UNSAFE.search(content):
        raise ValueError("Detected unsafe public output; publication stopped")


def _public_x_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or (parsed.hostname or "").casefold() not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
    ):
        raise ValueError("Dashboard post must use an HTTPS X URL")
    return value


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

    def build(self) -> dict[str, Any]:
        now = self._clock()
        posts = [post for _, post in self.markdown.iter_posts()]
        ranked = rank_posts(
            posts,
            now=now,
            timezone_name=self.config.timezone,
            configured_keywords=self.config.keywords,
        )
        if not ranked:
            raise ValueError("No valid dashboard candidates; previous public snapshot is unchanged")

        output = self.config.dashboard_dir
        media_count = 0
        public_posts: list[dict[str, Any]] = []
        for item in ranked:
            public_media = self._export_media(item, output)
            media_count += len(public_media)
            public_posts.append(self._public_post(item, public_media))

        topics = []
        for topic in TOPICS:
            matching = [item for item in ranked if item.topic.id == topic.id]
            topics.append(
                {
                    "id": topic.id,
                    "label": topic.label,
                    "family": topic.family,
                    "posts": len(matching),
                    "score": round(sum(item.score for item in matching) / len(matching), 6) if matching else 0.0,
                    "top_keyword": self._top_keyword(matching),
                }
            )

        generated_at = now.isoformat().replace("+00:00", "Z")
        snapshot = {
            "version": 1,
            "generated_at": generated_at,
            "timezone": self.config.timezone,
            "fallback_used": any(item.fallback for item in ranked),
            "summary": {
                "posts": len(public_posts),
                "authors": len({item.post.author.casefold() for item in ranked}),
                "media": media_count,
                "engagement": sum(item.post.likes + item.post.views for item in ranked),
            },
            "topics": topics,
            "posts": public_posts,
        }
        encoded = (json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        assert_public_content(encoded.decode("utf-8"))
        self._copy_static_assets(output)
        stamp = now.astimezone(ZoneInfo(self.config.timezone)).strftime("%Y-%m-%dT%H%M%S%z")
        dated = output / "data" / f"{stamp}.json"
        _atomic_write(dated, encoded)
        _atomic_write(output / "data" / "latest.json", encoded)
        return {"output": str(output), "snapshot": str(dated), "posts": len(public_posts), "media": media_count}

    def _public_post(self, item: RankedPost, media: list[dict[str, str]]) -> dict[str, Any]:
        post = item.post
        return {
            "id": post.id,
            "author": post.author,
            "text": post.text,
            "created_at": post.created_at,
            "url": _public_x_url(post.url),
            "likes": post.likes,
            "views": post.views,
            "topic": item.topic.id,
            "family": item.topic.family,
            "keywords": list(post.source_keywords),
            "score": round(item.score, 6),
            "fallback": item.fallback,
            "media": media,
        }

    def _export_media(self, item: RankedPost, output: Path) -> list[dict[str, str]]:
        exported: list[dict[str, str]] = []
        media_root = self.config.media_dir.resolve()
        for media in item.post.local_media:
            source = (self.config.markdown_dir / media.relative_path).resolve()
            if source == media_root or media_root not in source.parents:
                raise ValueError(f"Unsafe dashboard media path for post {item.post.id}")
            suffix = source.suffix.lower()
            signatures = _IMAGE_SIGNATURES.get(suffix)
            if signatures is None or not source.is_file():
                continue
            payload = source.read_bytes()
            if suffix == ".webp":
                valid = payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
            else:
                valid = any(payload.startswith(signature) for signature in signatures)
            if not valid:
                continue
            digest = hashlib.sha256(payload).hexdigest()
            relative = Path("assets") / "media" / f"{digest}{suffix}"
            target = output / relative
            if not target.exists():
                _atomic_write(target, payload)
            exported.append(
                {
                    "url": relative.as_posix(),
                    "type": media.kind,
                    "alt": item.post.text.strip()[:160] or f"@{item.post.author} 的配图",
                }
            )
        return exported

    def _copy_static_assets(self, output: Path) -> None:
        source = self.config.dashboard_source_dir
        required = (source / "index.html", source / "assets" / "styles.css", source / "assets" / "app.js")
        if not all(path.is_file() for path in required):
            raise ValueError("Dashboard static source is incomplete")
        for path in required:
            relative = path.relative_to(source)
            _atomic_write(output / relative, path.read_bytes())
        _atomic_write(output / ".nojekyll", b"")

    @staticmethod
    def _top_keyword(items: list[RankedPost]) -> str:
        counts = Counter(keyword for item in items for keyword in item.post.source_keywords)
        if not counts:
            return ""
        value = counts.most_common(1)[0][0]
        quoted = re.search(r'"([^"]+)"', value)
        label = quoted.group(1) if quoted else re.split(r"\s+OR\s+", value, maxsplit=1, flags=re.IGNORECASE)[0]
        return label.strip()[:48]

```

Keep `_atomic_write` private to this module. The implementation must use the exact-file atomic replacement above and must not add directory deletion or `shutil.rmtree`.

- [ ] **Step 4: Run exporter tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_dashboard_scoring.py tests/test_dashboard_export.py -v
```

Expected: all tests pass and source Markdown/media bytes remain unchanged.

- [ ] **Step 5: Commit the exporter slice**

```bash
git add src/xrag/dashboard_export.py tests/test_dashboard_export.py
git commit -m "feat: export safe dashboard snapshots"
```

## Task 3: Build the white data-cockpit front end

**Files:**
- Create: `dashboard/index.html`
- Create: `dashboard/assets/styles.css`
- Create: `dashboard/assets/app.js`
- Create: `dashboard/tests/app.test.mjs`
- Create: `tests/test_dashboard_assets.py`

- [ ] **Step 1: Write failing static-contract tests**

Create `tests/test_dashboard_assets.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_html_has_accessible_regions_templates_and_module() -> None:
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    for marker in (
        'id="refresh-button"', 'id="lead-story"', 'id="summary-grid"',
        'id="topic-grid"', 'id="hotspot-feed"', 'id="post-dialog"',
        'id="post-template"', 'type="module"',
    ):
        assert marker in html
    assert "立即刷新" in html


def test_styles_are_white_first_pastel_and_responsive() -> None:
    css = (ROOT / "dashboard" / "assets" / "styles.css").read_text(encoding="utf-8")
    assert "--page: #f8fafc" in css
    assert "--card: #ffffff" in css
    assert "@media (max-width: 760px)" in css
    assert "prefers-reduced-motion" in css


def test_javascript_uses_safe_text_rendering_and_cache_busted_refresh() -> None:
    script = (ROOT / "dashboard" / "assets" / "app.js").read_text(encoding="utf-8")
    assert "textContent" in script
    assert "innerHTML" not in script
    assert 'data/latest.json?t=' in script
    assert "export function sortPosts" in script
```

Create `dashboard/tests/app.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { formatMetric, isStale, sortPosts, snapshotUrl } from "../assets/app.js";

test("sortPosts supports score, newest, and engagement without mutating input", () => {
  const posts = [
    {id: "a", score: 0.5, created_at: "2026-08-10T01:00:00Z", views: 10, likes: 2},
    {id: "b", score: 0.8, created_at: "2026-08-10T00:00:00Z", views: 1, likes: 1},
  ];
  assert.deepEqual(sortPosts(posts, "score").map((post) => post.id), ["b", "a"]);
  assert.deepEqual(sortPosts(posts, "newest").map((post) => post.id), ["a", "b"]);
  assert.deepEqual(sortPosts(posts, "engagement").map((post) => post.id), ["a", "b"]);
  assert.deepEqual(posts.map((post) => post.id), ["a", "b"]);
});

test("formatMetric and refresh URL are deterministic", () => {
  assert.equal(formatMetric(999), "999");
  assert.equal(formatMetric(12500), "12.5K");
  assert.equal(snapshotUrl(123), "data/latest.json?t=123");
});

test("stale state uses a 26 hour allowance", () => {
  assert.equal(isStale("2026-08-10T00:00:00Z", new Date("2026-08-11T01:00:00Z")), false);
  assert.equal(isStale("2026-08-10T00:00:00Z", new Date("2026-08-11T03:00:00Z")), true);
});
```

- [ ] **Step 2: Run static tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_dashboard_assets.py -v
node --test dashboard/tests/app.test.mjs
```

Expected: both commands fail because the dashboard assets do not exist.

- [ ] **Step 3: Create semantic page markup**

Create `dashboard/index.html` with no inline post data and no third-party scripts:

```html
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="X-RAG 每日 AI 与 Web3 热点看板">
  <title>X-RAG 今日热点</title>
  <link rel="stylesheet" href="assets/styles.css">
</head>
<body>
  <header class="site-header">
    <a class="brand" href="./" aria-label="X-RAG 今日热点首页"><span class="brand-mark">X</span><span>今日热点</span></a>
    <div class="header-actions">
      <span id="updated-at" class="updated-at">正在载入…</span>
      <button id="refresh-button" class="refresh-button" type="button">立即刷新</button>
    </div>
  </header>
  <main>
    <section id="status-banner" class="status-banner" aria-live="polite" hidden></section>
    <section id="lead-story" class="lead-story" aria-labelledby="lead-heading"><p class="loading">正在载入今日头条…</p></section>
    <section aria-labelledby="summary-heading"><div class="section-heading"><div><p class="eyebrow">TODAY AT A GLANCE</p><h2 id="summary-heading">今日概览</h2></div></div><div id="summary-grid" class="summary-grid"></div></section>
    <section aria-labelledby="topic-heading"><div class="section-heading"><div><p class="eyebrow">TREND SIGNALS</p><h2 id="topic-heading">主题趋势</h2></div></div><div id="topic-grid" class="topic-grid"></div></section>
    <section aria-labelledby="feed-heading">
      <div class="section-heading feed-heading"><div><p class="eyebrow">HOT POSTS</p><h2 id="feed-heading">热点内容</h2></div><div class="filters" role="group" aria-label="内容筛选"><button class="filter is-active" data-filter="all">全部</button><button class="filter" data-filter="AI">AI</button><button class="filter" data-filter="Web3">Web3</button><select id="sort-select" aria-label="排序方式"><option value="score">综合热度</option><option value="newest">最新发布</option><option value="engagement">互动最高</option></select></div></div>
      <div id="hotspot-feed" class="hotspot-feed"></div>
    </section>
  </main>
  <dialog id="post-dialog"><button id="dialog-close" class="dialog-close" aria-label="关闭详情">×</button><article id="dialog-content"></article></dialog>
  <template id="post-template"><article class="post-card"><div class="post-media"></div><div class="post-body"><div class="post-meta"><span class="topic-pill"></span><time></time></div><h3 class="post-author"></h3><p class="post-text"></p><div class="metrics"></div><button class="detail-button" type="button">查看详情</button></div></article></template>
  <script type="module" src="assets/app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Create the approved light visual system**

Create `dashboard/assets/styles.css`. Use these complete tokens and component rules; keep every foreground/background pair at WCAG AA contrast:

```css
:root{--page:#f8fafc;--card:#ffffff;--ink:#17212b;--muted:#6f7c86;--line:#e4e9ed;--green:#e8f3ee;--green-ink:#376a57;--blue:#edf5ff;--blue-ink:#3c668f;--purple:#f4efff;--purple-ink:#69528d;--orange:#fff5e8;--orange-ink:#8b6335;--shadow:0 12px 36px rgba(37,55,68,.08);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;color:var(--ink);background:var(--page)}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink)}button,select{font:inherit}button{cursor:pointer}.site-header{position:sticky;top:0;z-index:20;display:flex;align-items:center;justify-content:space-between;padding:16px max(24px,calc((100vw - 1180px)/2));background:rgba(248,250,252,.92);border-bottom:1px solid var(--line);backdrop-filter:blur(16px)}.brand{display:flex;gap:10px;align-items:center;color:var(--ink);font-weight:800;text-decoration:none}.brand-mark{display:grid;place-items:center;width:30px;height:30px;border-radius:9px;background:var(--green);color:var(--green-ink)}.header-actions{display:flex;align-items:center;gap:14px}.updated-at{font-size:13px;color:var(--muted)}.refresh-button,.filter,.detail-button,select{border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);padding:9px 14px}.refresh-button:hover,.filter:hover,.filter.is-active,.detail-button:hover{border-color:#99bfae;background:var(--green)}main{max-width:1180px;margin:auto;padding:34px 24px 64px}.status-banner{margin-bottom:18px;padding:12px 16px;border:1px solid #efd7ac;border-radius:12px;background:var(--orange);color:var(--orange-ink)}section{margin-bottom:42px}.lead-story{min-height:330px;display:grid;grid-template-columns:1.1fr .9fr;overflow:hidden;border:1px solid var(--line);border-radius:24px;background:var(--card);box-shadow:var(--shadow)}.lead-copy{display:flex;flex-direction:column;justify-content:center;padding:42px}.lead-copy h1{font-size:clamp(30px,5vw,54px);line-height:1.05;letter-spacing:-.04em;margin:14px 0}.lead-copy p{color:var(--muted);line-height:1.7}.lead-media{min-height:330px;background:linear-gradient(135deg,var(--green),var(--blue));overflow:hidden}.lead-media img{width:100%;height:100%;object-fit:cover}.eyebrow{margin:0 0 6px;color:var(--green-ink);font:700 12px ui-monospace,monospace;letter-spacing:.12em}.section-heading{display:flex;justify-content:space-between;align-items:end;margin-bottom:16px}.section-heading h2{font-size:28px;letter-spacing:-.03em;margin:0}.summary-grid,.topic-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.summary-card,.topic-card{min-height:128px;padding:20px;border:1px solid var(--line);border-radius:18px;background:var(--card)}.summary-card:nth-child(1),.topic-card:nth-child(1){background:var(--green)}.summary-card:nth-child(2),.topic-card:nth-child(2){background:var(--blue)}.summary-card:nth-child(3),.topic-card:nth-child(3){background:var(--purple)}.summary-card:nth-child(4),.topic-card:nth-child(4){background:var(--orange)}.summary-card strong{display:block;font-size:32px;margin-top:14px}.summary-card span,.topic-card p{color:var(--muted);font-size:13px}.topic-card h3{font-size:16px;margin:10px 0}.topic-score{font-size:28px;font-weight:800}.feed-heading{gap:18px}.filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.hotspot-feed{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.post-card{display:flex;flex-direction:column;min-width:0;overflow:hidden;border:1px solid var(--line);border-radius:18px;background:var(--card);box-shadow:0 6px 20px rgba(37,55,68,.05)}.post-media{aspect-ratio:16/9;background:linear-gradient(135deg,var(--blue),var(--green));overflow:hidden}.post-media:empty{display:none}.post-media img{width:100%;height:100%;object-fit:cover}.post-body{display:flex;flex:1;flex-direction:column;padding:18px}.post-meta{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px}.topic-pill{padding:4px 8px;border-radius:999px;background:var(--green);color:var(--green-ink)}.post-author{font-size:16px;margin:14px 0 8px}.post-text{display:-webkit-box;overflow:hidden;color:#4e5b65;line-height:1.6;-webkit-line-clamp:4;-webkit-box-orient:vertical}.metrics{display:flex;gap:16px;margin-top:auto;padding:14px 0;color:var(--muted);font-size:13px}.detail-button{align-self:flex-start}.loading{margin:auto;color:var(--muted)}dialog{width:min(720px,calc(100% - 32px));max-height:88vh;padding:30px;border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}dialog::backdrop{background:rgba(23,33,43,.35);backdrop-filter:blur(4px)}.dialog-close{float:right;width:36px;height:36px;border:0;border-radius:50%;background:var(--page);font-size:24px}.dialog-gallery{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.dialog-gallery img{width:100%;border-radius:12px}.source-link{color:var(--green-ink);font-weight:700}.empty{grid-column:1/-1;padding:36px;text-align:center;color:var(--muted);border:1px dashed var(--line);border-radius:18px}
.post-media:empty{display:block}
@media (max-width:900px){.summary-grid,.topic-grid{grid-template-columns:repeat(2,1fr)}.hotspot-feed{grid-template-columns:repeat(2,1fr)}}
@media (max-width:760px){.site-header{padding:13px 16px}.updated-at{display:none}main{padding:22px 16px 48px}.lead-story{grid-template-columns:1fr}.lead-copy{padding:26px}.lead-media{min-height:230px}.section-heading,.feed-heading{align-items:flex-start;flex-direction:column}.summary-grid,.topic-grid,.hotspot-feed{grid-template-columns:1fr}.filters{width:100%}.dialog-gallery{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition:none!important;animation:none!important}}
```

- [ ] **Step 5: Implement safe rendering and refresh behavior**

Create `dashboard/assets/app.js` as a browser module. All remote text must be assigned through `textContent`, and external links must receive `target="_blank"` plus `rel="noopener noreferrer"`:

```javascript
let snapshot = null;
let activeFilter = "all";
let activeSort = "score";

export function formatMetric(value) {
  const number = Number(value || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1).replace(".0", "")}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(1).replace(".0", "")}K`;
  return String(number);
}

export function sortPosts(posts, mode) {
  const copy = [...posts];
  if (mode === "newest") return copy.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  if (mode === "engagement") return copy.sort((a, b) => (b.views + b.likes) - (a.views + a.likes));
  return copy.sort((a, b) => b.score - a.score);
}

export function snapshotUrl(cacheKey = Date.now()) {
  return `data/latest.json?t=${cacheKey}`;
}

export function isStale(generatedAt, now = new Date()) {
  return now.getTime() - Date.parse(generatedAt) > 26 * 60 * 60 * 1000;
}

function text(tag, value, className = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}

function imageFor(post, className = "") {
  const media = post.media?.[0];
  if (!media) return null;
  const image = document.createElement("img");
  image.className = className;
  image.src = media.url;
  image.alt = media.alt || "热点配图";
  image.loading = "lazy";
  image.addEventListener("error", () => image.remove());
  return image;
}

function renderLead(post) {
  const root = document.querySelector("#lead-story");
  root.replaceChildren();
  const copy = document.createElement("div");
  copy.className = "lead-copy";
  copy.append(text("p", post.family === "AI" ? "AI 今日头条" : "WEB3 今日头条", "eyebrow"));
  copy.append(text("h1", post.text.length > 90 ? `${post.text.slice(0, 90)}…` : post.text));
  copy.append(text("p", `@${post.author} · ${new Date(post.created_at).toLocaleString("zh-CN")}`));
  const link = text("a", "查看原帖 ↗", "source-link");
  link.href = post.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  copy.append(link);
  const media = document.createElement("div");
  media.className = "lead-media";
  const image = imageFor(post);
  if (image) media.append(image);
  root.append(copy, media);
}

function renderSummary(value) {
  const labels = [
    ["入选热点", value.posts], ["活跃作者", value.authors],
    ["展示图片", value.media], ["总互动量", formatMetric(value.engagement)],
  ];
  const root = document.querySelector("#summary-grid");
  root.replaceChildren(...labels.map(([label, metric]) => {
    const card = document.createElement("article");
    card.className = "summary-card";
    card.append(text("span", label), text("strong", metric));
    return card;
  }));
}

function renderTopics(topics) {
  const root = document.querySelector("#topic-grid");
  root.replaceChildren(...topics.map((topic) => {
    const card = document.createElement("article");
    card.className = "topic-card";
    card.append(text("span", topic.family, "eyebrow"));
    card.append(text("h3", topic.label));
    card.append(text("div", `${Math.round(topic.score * 100)}`, "topic-score"));
    card.append(text("p", `${topic.posts} 条热点 · ${topic.top_keyword || "暂无关键词"}`));
    return card;
  }));
}

function openDetails(post) {
  const root = document.querySelector("#dialog-content");
  root.replaceChildren(text("p", post.family, "eyebrow"), text("h2", `@${post.author}`), text("p", post.text));
  const gallery = document.createElement("div");
  gallery.className = "dialog-gallery";
  for (const media of post.media || []) {
    const image = document.createElement("img");
    image.src = media.url;
    image.alt = media.alt || "热点配图";
    image.loading = "lazy";
    gallery.append(image);
  }
  if (gallery.childElementCount) root.append(gallery);
  root.append(text("p", `浏览 ${formatMetric(post.views)} · 点赞 ${formatMetric(post.likes)}`));
  const link = text("a", "打开 X 原帖 ↗", "source-link");
  link.href = post.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  root.append(link);
  document.querySelector("#post-dialog").showModal();
}

function postCard(post) {
  const fragment = document.querySelector("#post-template").content.cloneNode(true);
  const card = fragment.querySelector(".post-card");
  const media = fragment.querySelector(".post-media");
  const image = imageFor(post);
  if (image) media.append(image);
  fragment.querySelector(".topic-pill").textContent = post.family;
  const time = fragment.querySelector("time");
  time.dateTime = post.created_at;
  time.textContent = new Date(post.created_at).toLocaleDateString("zh-CN");
  fragment.querySelector(".post-author").textContent = `@${post.author}`;
  fragment.querySelector(".post-text").textContent = post.text;
  fragment.querySelector(".metrics").textContent = `浏览 ${formatMetric(post.views)}　点赞 ${formatMetric(post.likes)}`;
  fragment.querySelector(".detail-button").addEventListener("click", () => openDetails(post));
  return card;
}

function renderFeed() {
  const filtered = snapshot.posts.filter((post) => activeFilter === "all" || post.family === activeFilter);
  const root = document.querySelector("#hotspot-feed");
  if (!filtered.length) {
    root.replaceChildren(text("p", "当前筛选条件下没有热点内容。", "empty"));
    return;
  }
  root.replaceChildren(...sortPosts(filtered, activeSort).map(postCard));
}

function renderStatus(value, refreshed) {
  const banner = document.querySelector("#status-banner");
  const stale = isStale(value.generated_at);
  banner.hidden = !(stale || value.fallback_used || refreshed);
  banner.textContent = stale ? "数据超过 26 小时未更新，当前展示上一次成功快照。" : value.fallback_used ? "今日内容不足，已补充最近 48 小时热点。" : refreshed ? "已获取最新发布数据。" : "";
}

async function loadSnapshot(refreshed = false) {
  const button = document.querySelector("#refresh-button");
  button.disabled = true;
  try {
    const response = await fetch(snapshotUrl(), {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    snapshot = await response.json();
    if (!Array.isArray(snapshot.posts) || !snapshot.posts.length) throw new Error("empty snapshot");
    document.querySelector("#updated-at").textContent = `更新于 ${new Date(snapshot.generated_at).toLocaleString("zh-CN")}`;
    renderStatus(snapshot, refreshed);
    renderLead(snapshot.posts[0]);
    renderSummary(snapshot.summary);
    renderTopics(snapshot.topics);
    renderFeed();
  } catch (error) {
    const banner = document.querySelector("#status-banner");
    banner.hidden = false;
    banner.textContent = "暂时无法读取热点数据，请稍后刷新。";
  } finally {
    button.disabled = false;
  }
}

if (typeof document !== "undefined") {
  document.querySelector("#refresh-button").addEventListener("click", () => loadSnapshot(true));
  document.querySelector("#sort-select").addEventListener("change", (event) => { activeSort = event.target.value; renderFeed(); });
  document.querySelectorAll(".filter").forEach((button) => button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("is-active", item === button));
    renderFeed();
  }));
  document.querySelector("#dialog-close").addEventListener("click", () => document.querySelector("#post-dialog").close());
  loadSnapshot();
}
```

- [ ] **Step 6: Run Python and Node tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_dashboard_assets.py tests/test_dashboard_export.py -v
node --check dashboard/assets/app.js
node --test dashboard/tests/app.test.mjs
```

Expected: all tests pass and Node reports no syntax errors.

- [ ] **Step 7: Commit the UI slice**

```bash
git add dashboard tests/test_dashboard_assets.py
git commit -m "feat: add public hotspot dashboard UI"
```

## Task 4: Add a non-destructive GitHub Pages publisher

**Files:**
- Create: `src/xrag/dashboard_publish.py`
- Create: `tests/test_dashboard_publish.py`

- [ ] **Step 1: Write failing publisher tests with an injected command runner**

Create `tests/test_dashboard_publish.py`:

```python
from pathlib import Path
import subprocess

import pytest

from xrag.dashboard_publish import PagesPublisher


class Runner:
    def __init__(self, responses: dict[tuple[str, ...], subprocess.CompletedProcess[str]] | None = None) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, str | None]] = []
        self.responses = responses or {}

    def __call__(self, command: list[str], cwd: Path, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        self.calls.append((key, cwd, input_text))
        if key == ("git", "branch", "--show-current"):
            return subprocess.CompletedProcess(command, 0, "gh-pages\n", "")
        return self.responses.get(key, subprocess.CompletedProcess(command, 0, "", ""))


def site(root: Path) -> Path:
    path = root / "data" / "dashboard-site"
    (path / "data").mkdir(parents=True)
    (path / "assets").mkdir()
    (path / "index.html").write_text("safe", encoding="utf-8")
    (path / ".nojekyll").write_text("", encoding="utf-8")
    (path / "data" / "latest.json").write_text('{"version":1}', encoding="utf-8")
    (path / "assets" / "app.js").write_text("safe", encoding="utf-8")
    return path


def test_publish_initializes_empty_branch_without_removing_files_and_pushes(tmp_path: Path) -> None:
    runner = Runner({
        ("git", "show-ref", "--verify", "--quiet", "refs/heads/gh-pages"): subprocess.CompletedProcess([], 1, "", ""),
        ("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/gh-pages"): subprocess.CompletedProcess([], 1, "", ""),
        ("git", "mktree"): subprocess.CompletedProcess([], 0, "tree-hash\n", ""),
        ("git", "commit-tree", "tree-hash", "-m", "chore: initialize dashboard pages"): subprocess.CompletedProcess([], 0, "commit-hash\n", ""),
        ("git", "diff", "--cached", "--quiet"): subprocess.CompletedProcess([], 1, "", ""),
    })
    publisher = PagesPublisher(tmp_path, tmp_path / ".worktrees" / "x-rag-pages", runner=runner, clock=lambda: "2026-08-10T12:00:00+08:00")

    result = publisher.publish(site(tmp_path))

    commands = [call[0] for call in runner.calls]
    assert not any(command[:2] in {("git", "rm"), ("git", "clean")} for command in commands)
    assert (("git", "mktree"), tmp_path.resolve(), "") in runner.calls
    assert ("git", "branch", "gh-pages", "commit-hash") in commands
    assert ("git", "worktree", "add", str(tmp_path / ".worktrees" / "x-rag-pages"), "gh-pages") in commands
    assert ("git", "add", "--", ".nojekyll", "index.html", "assets", "data") in commands
    assert ("git", "push", "origin", "gh-pages") in commands
    assert result["changed"] is True


def test_publish_skips_commit_and_push_when_generated_files_are_unchanged(tmp_path: Path) -> None:
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    worktree.mkdir(parents=True)
    runner = Runner({("git", "diff", "--cached", "--quiet"): subprocess.CompletedProcess([], 0, "", "")})

    result = PagesPublisher(tmp_path, worktree, runner=runner).publish(site(tmp_path))

    commands = [call[0] for call in runner.calls]
    assert not any(command[:2] == ("git", "commit") for command in commands)
    assert ("git", "push", "origin", "gh-pages") not in commands
    assert result["changed"] is False


def test_publish_rejects_source_outside_project_and_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-dashboard"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="site directory"):
        PagesPublisher(tmp_path, tmp_path / ".worktrees" / "x-rag-pages", runner=Runner()).publish(outside)

    source = site(tmp_path)
    try:
        (source / "assets" / "escape").symlink_to(tmp_path.parent)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="symlink"):
        PagesPublisher(tmp_path, tmp_path / ".worktrees" / "x-rag-pages", runner=Runner()).publish(source)


def test_publish_rescans_generated_text_before_running_git(tmp_path: Path) -> None:
    source = site(tmp_path)
    (source / "data" / "latest.json").write_text('{"text":"ct0=secret"}', encoding="utf-8")
    runner = Runner()

    with pytest.raises(ValueError, match="unsafe public output"):
        PagesPublisher(tmp_path, tmp_path / ".worktrees" / "x-rag-pages", runner=runner).publish(source)

    assert runner.calls == []
```

- [ ] **Step 2: Run publisher tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_dashboard_publish.py -v
```

Expected: collection fails because `PagesPublisher` does not exist.

- [ ] **Step 3: Implement safe branch/worktree and copy behavior**

Create `src/xrag/dashboard_publish.py` with an injected runner and these guarantees: resolve and bound all paths, reject symlinks, create a truly empty initial branch through `git mktree`/`git commit-tree`, copy without `--delete`, stage only known public roots, and never call `git rm`, `git clean`, reset, or checkout restoration.

```python
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from .dashboard_export import assert_public_content

Runner = Callable[[list[str], Path, str | None], subprocess.CompletedProcess[str]]


def _run(command: list[str], cwd: Path, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, input=input_text, text=True, encoding="utf-8", capture_output=True, check=False)


class PagesPublisher:
    def __init__(self, root: Path, worktree: Path, *, runner: Runner = _run, clock: Callable[[], str] | None = None) -> None:
        self.root = root.resolve()
        self.worktree = worktree.resolve()
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat())

    def publish(self, site_dir: Path) -> dict[str, Any]:
        source = site_dir.resolve()
        expected = (self.root / "data" / "dashboard-site").resolve()
        if source != expected or not source.is_dir():
            raise ValueError("Dashboard site directory must be the configured generated directory")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise ValueError("Dashboard publication refuses symlink content")
        for required in (source / "index.html", source / ".nojekyll", source / "data" / "latest.json"):
            if not required.is_file():
                raise ValueError(f"Dashboard publication is missing {required.name}")
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".html", ".css", ".js", ".json"}:
                assert_public_content(path.read_text(encoding="utf-8"))

        self._ensure_worktree()
        for name in ("index.html", ".nojekyll"):
            self._copy_file(source / name, self.worktree / name)
        for directory in ("assets", "data"):
            for path in sorted((source / directory).rglob("*")):
                if path.is_file():
                    self._copy_file(path, self.worktree / path.relative_to(source))

        self._checked(["git", "add", "--", ".nojekyll", "index.html", "assets", "data"], self.worktree)
        changed = self._runner(["git", "diff", "--cached", "--quiet"], self.worktree, None).returncode != 0
        if not changed:
            return {"changed": False, "branch": "gh-pages"}
        self._checked(["git", "commit", "-m", f"data: publish dashboard {self._clock()}"], self.worktree)
        self._checked(["git", "push", "origin", "gh-pages"], self.worktree)
        return {"changed": True, "branch": "gh-pages"}

    def _ensure_worktree(self) -> None:
        if self.worktree.is_dir():
            branch = self._checked(["git", "branch", "--show-current"], self.worktree).stdout.strip()
            if branch != "gh-pages":
                raise RuntimeError("Dashboard Pages worktree is not on gh-pages")
            return
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        local = self._runner(["git", "show-ref", "--verify", "--quiet", "refs/heads/gh-pages"], self.root, None)
        if local.returncode != 0:
            remote = self._runner(["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/gh-pages"], self.root, None)
            if remote.returncode == 0:
                self._checked(["git", "branch", "gh-pages", "origin/gh-pages"], self.root)
            else:
                tree = self._checked(["git", "mktree"], self.root, input_text="").stdout.strip()
                commit = self._checked(["git", "commit-tree", tree, "-m", "chore: initialize dashboard pages"], self.root).stdout.strip()
                self._checked(["git", "branch", "gh-pages", commit], self.root)
        self._checked(["git", "worktree", "add", str(self.worktree), "gh-pages"], self.root)

    def _checked(self, command: list[str], cwd: Path, *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        result = self._runner(command, cwd, input_text)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "command failed").splitlines()[0]
            raise RuntimeError(f"Dashboard publish command failed: {message}")
        return result

    @staticmethod
    def _copy_file(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
```

The stdin-aware runner above is the single command-execution path, including `git mktree`; do not add a direct `subprocess.run` bypass inside `PagesPublisher`.

- [ ] **Step 4: Run publisher tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_dashboard_publish.py -v
```

Expected: all tests pass; no test command includes `rm`, `clean`, `reset`, or destructive checkout operations.

- [ ] **Step 5: Commit the publisher slice**

```bash
git add src/xrag/dashboard_publish.py tests/test_dashboard_publish.py
git commit -m "feat: publish dashboard without destructive sync"
```

## Task 5: Expose build, publish, and update CLI commands

**Files:**
- Modify: `src/xrag/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Extend the CLI fake and write failing command tests**

Add small fakes to `tests/test_cli.py`:

```python
class FakeDashboardBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build(self) -> dict[str, object]:
        self.calls += 1
        return {"output": "data/dashboard-site", "posts": 8, "media": 4}


class FakePublisher:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def publish(self, path: Path) -> dict[str, object]:
        self.paths.append(path)
        return {"changed": True, "branch": "gh-pages"}
```

Add tests using `monkeypatch.setattr(cli, "build_dashboard", ...)` and `build_pages_publisher`:

```python
def test_dashboard_build_does_not_initialize_xrag_service(monkeypatch, tmp_path: Path) -> None:
    dashboard = FakeDashboardBuilder()
    monkeypatch.setattr(cli, "build_dashboard", lambda root: dashboard)
    monkeypatch.setattr(cli, "build_service", lambda root: pytest.fail("service must stay lazy"))

    result = runner.invoke(cli.app, ["--root", str(tmp_path), "dashboard", "build"])

    assert result.exit_code == 0
    assert dashboard.calls == 1
    assert json.loads(result.stdout)["posts"] == 8


def test_dashboard_publish_builds_then_pushes(monkeypatch, tmp_path: Path) -> None:
    dashboard = FakeDashboardBuilder()
    publisher = FakePublisher()
    monkeypatch.setattr(cli, "build_dashboard", lambda root: dashboard)
    monkeypatch.setattr(cli, "build_pages_publisher", lambda root: publisher)

    result = runner.invoke(cli.app, ["--root", str(tmp_path), "dashboard", "publish"])

    assert result.exit_code == 0
    assert dashboard.calls == 1
    assert publisher.paths == [tmp_path.resolve() / "data" / "dashboard-site"]


def test_dashboard_update_collects_before_build_and_publish(monkeypatch, tmp_path: Path) -> None:
    events: list[str] = []
    service = FakeService()
    service.collect_all = lambda: events.append("collect") or [("query", {"found": 1, "stored": 1, "chunks": 1, "errors": 0})]
    dashboard = FakeDashboardBuilder()
    dashboard.build = lambda: events.append("build") or {"output": str(tmp_path / "data" / "dashboard-site"), "posts": 1, "media": 0}
    publisher = FakePublisher()
    publisher.publish = lambda path: events.append("publish") or {"changed": True, "branch": "gh-pages"}
    monkeypatch.setattr(cli, "build_service", lambda root: service)
    monkeypatch.setattr(cli, "build_dashboard", lambda root: dashboard)
    monkeypatch.setattr(cli, "build_pages_publisher", lambda root: publisher)

    result = runner.invoke(cli.app, ["--root", str(tmp_path), "dashboard", "update"])

    assert result.exit_code == 0
    assert events == ["collect", "build", "publish"]


def test_dashboard_update_stops_before_build_when_collection_stores_nothing(monkeypatch, tmp_path: Path) -> None:
    service = FakeService()
    service.collect_all = lambda: [("query", {"found": 0, "stored": 0, "chunks": 0, "errors": 1})]
    monkeypatch.setattr(cli, "build_service", lambda root: service)
    monkeypatch.setattr(cli, "build_dashboard", lambda root: pytest.fail("build must not run"))

    result = runner.invoke(cli.app, ["--root", str(tmp_path), "dashboard", "update"])

    assert result.exit_code == 2
    assert "stored no posts" in result.stderr
```

- [ ] **Step 2: Run focused CLI tests and verify RED**

```bash
.venv/bin/python -m pytest tests/test_cli.py -k dashboard -v
```

Expected: tests fail because the dashboard Typer group and factories do not exist.

- [ ] **Step 3: Add lazy factories and Typer subcommands**

Modify imports and app setup in `src/xrag/cli.py`:

```python
from .dashboard_export import DashboardBuilder
from .dashboard_publish import PagesPublisher

dashboard_app = typer.Typer(no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")


def build_dashboard(root: Path) -> DashboardBuilder:
    config = load_config(root.resolve())
    return DashboardBuilder(config, MarkdownStore(config.markdown_dir))


def build_pages_publisher(root: Path) -> PagesPublisher:
    config = load_config(root.resolve())
    return PagesPublisher(config.root, config.pages_worktree)
```

Add the commands before `_print_json`:

```python
@dashboard_app.command("build")
def dashboard_build(ctx: typer.Context) -> None:
    _print_json(_run(lambda: build_dashboard(ctx.obj).build()))


def _build_and_publish(root: Path) -> dict[str, object]:
    build_result = build_dashboard(root).build()
    publish_result = build_pages_publisher(root).publish(root.resolve() / "data" / "dashboard-site")
    return {"build": build_result, "publish": publish_result}


@dashboard_app.command("publish")
def dashboard_publish(ctx: typer.Context) -> None:
    _print_json(_run(lambda: _build_and_publish(ctx.obj)))


def _collect_build_publish(root: Path) -> dict[str, object]:
    collection = build_service(root).collect_all()
    if sum(counts["stored"] for _, counts in collection) == 0:
        raise RuntimeError("Collection stored no posts; dashboard publication stopped")
    result = _build_and_publish(root)
    return {"collection": collection, **result}


@dashboard_app.command("update")
def dashboard_update(ctx: typer.Context) -> None:
    _print_json(_run(lambda: _collect_build_publish(ctx.obj)))
```

Ensure `_run` continues to redact all raised `RuntimeError`, `ValueError`, `OSError`, YAML, and OpenCLI errors and emits no traceback.

- [ ] **Step 4: Run all CLI tests and verify GREEN**

```bash
.venv/bin/python -m pytest tests/test_cli.py -v
.venv/bin/xrag --help
.venv/bin/xrag dashboard --help
```

Expected: tests pass; root help lists `dashboard`; dashboard help lists `build`, `publish`, and `update`.

- [ ] **Step 5: Commit the CLI slice**

```bash
git add src/xrag/cli.py tests/test_cli.py
git commit -m "feat: add dashboard CLI workflow"
```

## Task 6: Connect the 10:00 scheduled task to validated publication

**Files:**
- Modify: `scripts/run-daily.sh`
- Modify: `tests/test_scheduler_scripts.py`

- [ ] **Step 1: Write a failing scheduler assertion**

Update the daily-runner test in `tests/test_scheduler_scripts.py` so it requires exactly one dashboard update entry point:

```python
def test_daily_runner_uses_fail_fast_dashboard_update_pipeline() -> None:
    script = (ROOT / "scripts" / "run-daily.sh").read_text(encoding="utf-8")
    assert 'set -euo pipefail' in script
    assert 'dashboard update' in script
    assert 'collect --all' not in script
    assert 'exec >> "$PROJECT_ROOT/logs/scheduler.log" 2>&1' in script
```

- [ ] **Step 2: Run the scheduler test and verify RED**

```bash
.venv/bin/python -m pytest tests/test_scheduler_scripts.py -v
```

Expected: the new test fails because `run-daily.sh` still invokes `collect --all`.

- [ ] **Step 3: Replace only the final daily command**

Keep the existing root resolution, log redirection, virtual-environment validation, and start banner. Replace the final line in `scripts/run-daily.sh` with:

```bash
exec "$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" dashboard update
```

Do not alter `scripts/install-schedule.ps1`; the existing task name, 10:00 trigger, interactive logon type, WSL distribution, and force-update behavior remain correct.

- [ ] **Step 4: Run scheduler and shell syntax tests**

```bash
bash -n scripts/run-daily.sh
.venv/bin/python -m pytest tests/test_scheduler_scripts.py -v
```

Expected: shell syntax is valid and all scheduler tests pass.

- [ ] **Step 5: Commit the automation slice**

```bash
git add scripts/run-daily.sh tests/test_scheduler_scripts.py
git commit -m "feat: publish dashboard after daily collection"
```

## Task 7: Document operations and run the full local verification

**Files:**
- Modify: `README.md`
- Test: all Python and Node tests

- [ ] **Step 1: Add exact dashboard operating instructions**

Add a `## 公开热点看板` section to `README.md` covering these commands and meanings:

```bash
# 只读取现有 Markdown 并生成本地静态站点
xrag --root . dashboard build

# 本地预览；浏览器打开 http://localhost:8000
python -m http.server 8000 --directory data/dashboard-site

# 读取现有 Markdown、构建并发布到 gh-pages
xrag --root . dashboard publish

# 立即采集四组关键词、构建并发布
xrag --root . dashboard update
```

Document all of these facts explicitly:

- the public URL is `https://bitesiling-coder.github.io/xauto/` after Pages is enabled;
- the browser button reloads the latest published snapshot but does not remotely control the computer;
- `dashboard update` is the real immediate collection path;
- the daily Windows task runs the same update command at 10:00 while the user is logged in and the browser bridge is connected;
- source Markdown and media are read-only and are never removed by dashboard commands;
- `data/dashboard-site/` is generated and ignored by Git;
- the `gh-pages` worktree receives copied public files without delete synchronization;
- old dated snapshots and content-addressed assets accumulate and therefore disk/repository size should be monitored;
- any detected credential or local path aborts publication and preserves the current live snapshot;
- Git authentication must already allow `git push origin gh-pages`.

- [ ] **Step 2: Run the complete automated suite**

```bash
.venv/bin/python -m pytest -q
node --check dashboard/assets/app.js
node --test dashboard/tests/app.test.mjs
.venv/bin/python -m pip check
```

Expected: all pytest and Node tests pass; `pip check` reports no broken requirements.

- [ ] **Step 3: Build from the existing local archive and inspect for leaks**

```bash
.venv/bin/xrag --root . dashboard build
test -f data/dashboard-site/index.html
test -f data/dashboard-site/data/latest.json
! rg -n -i 'auth[_-]?token|ct0|authorization|[A-Z]:\\|/mnt/[a-z]/|/home/' data/dashboard-site
python -m json.tool data/dashboard-site/data/latest.json >/dev/null
```

Expected: the build succeeds with at least one post, required files exist, the leak scan returns no matches, and JSON validation succeeds.

- [ ] **Step 4: Preview and visually verify desktop and mobile layouts**

Run:

```bash
python -m http.server 8000 --directory data/dashboard-site
```

Open `http://localhost:8000` and verify:

- white page and pastel cards match the approved mockup;
- lead image, metrics, four topic cards, filters, sorting, details dialog, and source links work;
- refresh reports the latest snapshot;
- a missing image leaves a pastel placeholder;
- 390 px viewport is single-column with no horizontal overflow;
- keyboard focus reaches refresh, filters, sorting, card details, close, and source links;
- reduced-motion preference does not animate transitions.

Stop only this foreground preview server with `Ctrl+C`; do not remove generated files.

- [ ] **Step 5: Commit documentation after successful verification**

```bash
git add README.md
git commit -m "docs: explain dashboard publishing"
```

## Task 8: Enable GitHub Pages, publish once, and verify live delivery

**Files:**
- No source changes expected
- External state: GitHub repository Pages settings and `gh-pages` branch

- [ ] **Step 1: Verify GitHub identity, remote, branch, and clean worktree**

```bash
gh auth status
git remote get-url origin
git branch --show-current
git status --short
```

Expected: GitHub authentication is active; origin is `https://github.com/bitesiling-coder/xauto` or its SSH equivalent; branch is `codex/x-rag-dashboard`; status is clean.

- [ ] **Step 2: Push the dashboard feature branch**

```bash
git push -u origin codex/x-rag-dashboard
```

Expected: the remote branch is created or fast-forwarded successfully.

- [ ] **Step 3: Run the first safe Pages publication**

```bash
.venv/bin/xrag --root . dashboard publish
git -C .worktrees/x-rag-pages status --short
git -C .worktrees/x-rag-pages log -1 --oneline
```

Expected: the command reports `branch: gh-pages`; the Pages worktree is clean; the latest commit message starts with `data: publish dashboard`.

- [ ] **Step 4: Enable or update GitHub Pages branch deployment**

First inspect current state:

```bash
gh api repos/bitesiling-coder/xauto/pages
```

If the request returns 404, create Pages configuration:

```bash
gh api --method POST repos/bitesiling-coder/xauto/pages -f 'source[branch]=gh-pages' -f 'source[path]=/'
```

If Pages already exists, update it:

```bash
gh api --method PUT repos/bitesiling-coder/xauto/pages -f 'source[branch]=gh-pages' -f 'source[path]=/'
```

Expected: the response identifies `gh-pages` and `/` as the source. Do not modify repository visibility or authentication settings.

- [ ] **Step 5: Verify the live site and public payload**

After GitHub reports the Pages build complete, run:

```bash
curl -fsSL https://bitesiling-coder.github.io/xauto/ | rg 'X-RAG|今日热点'
curl -fsSL https://bitesiling-coder.github.io/xauto/data/latest.json > /tmp/xrag-dashboard-latest.json
python -m json.tool /tmp/xrag-dashboard-latest.json >/dev/null
! rg -n -i 'auth[_-]?token|ct0|authorization|[A-Z]:\\|/mnt/[a-z]/|/home/' /tmp/xrag-dashboard-latest.json
```

Expected: the page title/brand is present, live JSON parses, and the public payload contains no credential names or local paths. The temporary verification file is outside the project and need not be removed as part of this plan.

- [ ] **Step 6: Reinstall/update the existing 10:00 task only after live verification**

From Windows PowerShell in the normal project directory that owns the real scheduled task, preview first:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -Distribution Ubuntu -ScheduleTime "10:00" -DryRun
```

Confirm the action points to the intended normal project, then update the existing same-name task:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-schedule.ps1 -Distribution Ubuntu -ScheduleTime "10:00"
Get-ScheduledTask -TaskName 'X-RAG Daily Collection' | Format-List TaskName,State,Actions,Triggers,Principal
```

Expected: one task named `X-RAG Daily Collection` remains, its trigger is daily at 10:00, and its action runs the updated `scripts/run-daily.sh`. No other scheduled task or computer file is deleted.

## Final acceptance checklist

- [ ] Existing canonical Markdown and media file hashes are unchanged before and after dashboard build/publish tests.
- [ ] All Python tests, Node tests, shell syntax checks, and `pip check` pass.
- [ ] Generated JSON contains only the documented allowlist and relative media URLs.
- [ ] The dashboard is usable on desktop and a 390 px mobile viewport.
- [ ] Failed/empty collection and unsafe output leave the prior live snapshot unchanged.
- [ ] The `gh-pages` publisher never uses delete synchronization or destructive Git commands.
- [ ] The live GitHub Pages URL serves the white data-cockpit dashboard and valid current snapshot.
- [ ] Exactly one existing Windows scheduled task runs the update pipeline daily at 10:00.
