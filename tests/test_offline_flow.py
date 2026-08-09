from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.models import Post
from xrag.opencli import parse_search_yaml
from xrag.service import XragService


FIXTURE = Path(__file__).parent / "fixtures" / "opencli-search.yaml"
POST_ID = "2084640002085130466"
POST_URL = "https://x.com/0xQiYan/status/2084640002085130466"


class OfflineVectors:
    def __init__(self) -> None:
        self.cleared = 0
        self.indexed: list[tuple[Post, Path]] = []

    def clear(self) -> None:
        self.cleared += 1

    def index_post(self, post: Post, path: Path) -> int:
        self.indexed.append((post, path))
        return 1


def test_fixture_round_trips_to_canonical_markdown_and_rebuilds_offline(tmp_path: Path) -> None:
    [parsed] = parse_search_yaml(FIXTURE.read_text(encoding="utf-8"), "DDR5")
    markdown = MarkdownStore(
        tmp_path / "data" / "markdown",
        clock=lambda: "2026-08-09T12:00:00Z",
    )

    canonical_path = markdown.upsert(parsed)
    stored = list(markdown.iter_posts())

    assert len(stored) == 1
    assert canonical_path == tmp_path / "data" / "markdown" / f"{POST_ID}.md"
    assert stored == [(canonical_path, markdown.read(canonical_path))]
    post = stored[0][1]
    assert post.id == POST_ID
    assert post.source_keywords == ("DDR5",)
    assert post.url == POST_URL
    assert post.text == "DDR5 内存价格上涨，装机成本又要增加了。"
    assert post.media_urls == ("https://pbs.twimg.com/media/DDR5-example.jpg",)

    canonical = canonical_path.read_text(encoding="utf-8")
    for traceable_field in (
        f"id: '{POST_ID}'",
        "author: 0xQiYan",
        "created_at: '2026-08-08T10:30:00Z'",
        "collected_at: '2026-08-09T12:00:00Z'",
        "updated_at: '2026-08-09T12:00:00Z'",
        f"url: {POST_URL}",
        "source_keywords:",
        "- DDR5",
        "source_type: opencli",
    ):
        assert traceable_field in canonical
    lowered = canonical.casefold()
    assert "auth_token" not in lowered
    assert "ct0" not in lowered
    assert "credential" not in lowered

    config = AppConfig(
        root=tmp_path,
        schedule_enabled=False,
        schedule_time="10:00",
        timezone="Asia/Singapore",
        limit_per_keyword=50,
        delay_seconds=10,
        keywords=("DDR5",),
        embedding_model="offline-test-model",
    )
    vectors = OfflineVectors()
    service = XragService(config, opencli=None, markdown=markdown, vectors=vectors)

    assert service.rebuild() == {"documents": 1, "chunks": 1, "errors": 0}
    assert vectors.cleared == 1
    assert vectors.indexed == [(post, canonical_path)]
