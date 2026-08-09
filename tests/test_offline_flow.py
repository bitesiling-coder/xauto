from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

os.environ.setdefault("RAYON_NUM_THREADS", "1")

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import chromadb
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
    from chromadb.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.opencli import parse_search_yaml
from xrag.service import XragService
from xrag.vector_store import VectorStore


FIXTURE = Path(__file__).parent / "fixtures" / "opencli-search.yaml"
POST_ID = "2084640002085130466"
POST_URL = "https://x.com/0xQiYan/status/2084640002085130466"


class DeterministicEmbeddingFunction(EmbeddingFunction[Documents]):
    def __init__(self) -> None:
        pass

    @staticmethod
    def name() -> str:
        return "xrag-offline-test"

    def get_config(self) -> dict[str, object]:
        return {}

    @staticmethod
    def build_from_config(config: dict[str, object]) -> "DeterministicEmbeddingFunction":
        return DeterministicEmbeddingFunction()

    def __call__(self, input: Documents) -> Embeddings:
        return [[1.0, 0.0, 0.0] for _ in input]


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
    chroma = chromadb.EphemeralClient(
        settings=Settings(anonymized_telemetry=False, is_persistent=False)
    )
    collection = chroma.create_collection(
        "xrag-offline-flow",
        embedding_function=DeterministicEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"},
    )
    vectors = VectorStore(collection)
    service = XragService(config, opencli=None, markdown=markdown, vectors=vectors)

    assert service.rebuild() == {"documents": 1, "chunks": 1, "errors": 0}
    assert vectors.count() == 1
    [hit] = service.search("DDR5", top=1)
    assert hit.post_id == POST_ID
    assert hit.url == POST_URL
    assert hit.markdown_path == str(canonical_path)


def test_opencli_secrets_are_not_persisted_to_markdown(tmp_path: Path) -> None:
    payload = """
- id: secret-proof
  author: example
  text: safe public post
  url: https://x.com/example/status/secret-proof
  auth_token: AUTH_VALUE_MUST_NOT_PERSIST
  ct0: CT0_VALUE_MUST_NOT_PERSIST
  api_key: API_VALUE_MUST_NOT_PERSIST
"""
    [post] = parse_search_yaml(payload, "security")
    markdown = MarkdownStore(tmp_path / "markdown", clock=lambda: "2026-08-09T12:00:00Z")

    canonical = markdown.upsert(post).read_text(encoding="utf-8")

    for forbidden in (
        "auth_token",
        "ct0",
        "api_key",
        "AUTH_VALUE_MUST_NOT_PERSIST",
        "CT0_VALUE_MUST_NOT_PERSIST",
        "API_VALUE_MUST_NOT_PERSIST",
    ):
        assert forbidden.casefold() not in canonical.casefold()
