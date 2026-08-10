from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from xrag.models import Post, QuotedPost
from xrag.vector_store import VectorStore


class FakeCollection:
    def __init__(self):
        self.calls = []
        self.query_result = {}
        self.get_result = {"ids": []}
        self.total = 0

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def upsert(self, **kwargs):
        self.calls.append(("upsert", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return self.query_result

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return self.get_result

    def count(self):
        self.calls.append(("count", {}))
        return self.total


def make_post(text="甲乙丙丁戊己庚辛"):
    return Post(
        id="post-1",
        author="作者",
        text=text,
        created_at="2026-01-02T03:04:05Z",
        url="https://x.com/example/status/1",
    )


def test_index_post_upserts_stable_ids_and_metadata_before_stale_cleanup(tmp_path):
    collection = FakeCollection()
    collection.get_result = {"ids": ["post-1:0", "post-1:1", "post-1:2"]}
    store = VectorStore(collection, max_chars=5, overlap=1)
    markdown_path = tmp_path / "post-1.md"

    assert store.index_post(make_post(), markdown_path) == 2

    assert collection.calls[0] == (
        "get",
        {"where": {"post_id": "post-1"}, "include": []},
    )
    operation, payload = collection.calls[1]
    assert operation == "upsert"
    assert payload["ids"] == ["post-1:0", "post-1:1"]
    assert payload["documents"] == ["甲乙丙丁戊", "戊己庚辛"]
    assert payload["metadatas"] == [
        {
            "post_id": "post-1",
            "author": "作者",
            "created_at": "2026-01-02T03:04:05Z",
            "url": "https://x.com/example/status/1",
            "markdown_path": str(markdown_path),
        },
        {
            "post_id": "post-1",
            "author": "作者",
            "created_at": "2026-01-02T03:04:05Z",
            "url": "https://x.com/example/status/1",
            "markdown_path": str(markdown_path),
        },
    ]
    assert collection.calls[2] == ("delete", {"ids": ["post-1:2"]})


def test_index_post_includes_quoted_text_without_markdown_or_media(tmp_path):
    collection = FakeCollection()
    store = VectorStore(collection, max_chars=500, overlap=0)
    item = Post(
        "post-1",
        "author",
        "main body",
        "",
        "https://x.com/i/status/1",
        media_urls=("https://pbs.twimg.com/media/image",),
        quoted_post=QuotedPost(
            "2", "quoted", "quoted searchable body", "", "https://x.com/i/status/2"
        ),
    )

    assert store.index_post(item, tmp_path / "post-1.md") == 2

    indexed_text = "\n".join(collection.calls[1][1]["documents"])
    assert "main body" in indexed_text
    assert "quoted searchable body" in indexed_text
    assert "## 正文" not in indexed_text
    assert "![图片" not in indexed_text
    assert "pbs.twimg.com" not in indexed_text


def test_index_empty_post_only_removes_existing_chunks(tmp_path):
    collection = FakeCollection()
    collection.get_result = {"ids": ["post-1:0", "post-1:1"]}
    store = VectorStore(collection)

    assert store.index_post(make_post(" \n\n "), tmp_path / "post.md") == 0
    assert collection.calls == [
        ("get", {"where": {"post_id": "post-1"}, "include": []}),
        ("delete", {"ids": ["post-1:0", "post-1:1"]}),
    ]


def test_index_upsert_failure_preserves_existing_chunks(tmp_path):
    class BrokenUpsertCollection(FakeCollection):
        def upsert(self, **kwargs):
            self.calls.append(("upsert", kwargs))
            raise RuntimeError("write failed")

    collection = BrokenUpsertCollection()
    collection.get_result = {"ids": ["post-1:0", "post-1:1", "post-1:2"]}

    with pytest.raises(RuntimeError, match="write failed"):
        VectorStore(collection, max_chars=5, overlap=1).index_post(
            make_post(), tmp_path / "post.md"
        )

    assert [operation for operation, _ in collection.calls] == ["get", "upsert"]


@pytest.mark.parametrize("get_result", [None, {}, {"ids": "post-1:0"}, {"ids": [""]}])
def test_index_rejects_malformed_existing_ids_before_mutation(tmp_path, get_result):
    collection = FakeCollection()
    collection.get_result = get_result
    with pytest.raises(ValueError, match="Malformed Chroma get result"):
        VectorStore(collection).index_post(make_post(), tmp_path / "post.md")
    assert [operation for operation, _ in collection.calls] == ["get"]


def test_search_maps_first_result_set_and_clips_similarity():
    collection = FakeCollection()
    collection.query_result = {
        "ids": [["p1:0", "p2:0", "p3:0"]],
        "documents": [["一", "二", "三"]],
        "metadatas": [[
            {"post_id": "p1", "author": "甲", "created_at": "t1", "url": "u1", "markdown_path": "a.md"},
            {"post_id": "p2", "author": "乙", "created_at": "t2", "url": "u2", "markdown_path": "b.md"},
            {"post_id": "p3", "author": "丙", "created_at": "t3", "url": "u3", "markdown_path": "c.md"},
        ]],
        "distances": [[0.12344, -0.5, 2.0]],
    }
    store = VectorStore(collection)

    hits = store.search("查询", 3)

    assert collection.calls == [("query", {"query_texts": ["查询"], "n_results": 3})]
    assert [(hit.post_id, hit.text, hit.score) for hit in hits] == [
        ("p1", "一", 0.8766),
        ("p2", "二", 1.0),
        ("p3", "三", 0.0),
    ]
    assert hits[0].author == "甲"
    assert hits[0].markdown_path == "a.md"


def test_search_handles_empty_result():
    collection = FakeCollection()
    collection.query_result = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    assert VectorStore(collection).search("查询", 2) == []


@pytest.mark.parametrize(("query", "top"), [("", 1), ("   ", 1), ("ok", 0), ("ok", -1)])
def test_search_validates_inputs(query, top):
    with pytest.raises(ValueError):
        VectorStore(FakeCollection()).search(query, top)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"ids": 1, "documents": [[]], "metadatas": [[]], "distances": [[]]},
        {"ids": {"bad": []}, "documents": [[]], "metadatas": [[]], "distances": [[]]},
        {"ids": [], "documents": [], "metadatas": [], "distances": []},
        {"ids": [["p:0"]], "documents": [["text"]], "metadatas": [[{}]]},
        {"ids": [["p:0"]], "documents": [[]], "metadatas": [[{}]], "distances": [[0.1]]},
        {"ids": [["p:0"]], "documents": [["text"]], "metadatas": [], "distances": [[0.1]]},
    ],
)
def test_search_rejects_malformed_result_shapes(result):
    collection = FakeCollection()
    collection.query_result = result
    with pytest.raises(ValueError, match="Malformed Chroma query result"):
        VectorStore(collection).search("查询", 1)


@pytest.mark.parametrize("distance", ["bad", True, float("nan"), float("inf")])
def test_search_rejects_non_finite_or_non_numeric_distances(distance):
    collection = FakeCollection()
    collection.query_result = {
        "ids": [["p:0"]],
        "documents": [["text"]],
        "metadatas": [[{
            "post_id": "p",
            "author": "作者",
            "created_at": "time",
            "url": "url",
            "markdown_path": "p.md",
        }]],
        "distances": [[distance]],
    }
    with pytest.raises(ValueError, match="Malformed Chroma query result"):
        VectorStore(collection).search("查询", 1)


def test_count_delegates_to_collection():
    collection = FakeCollection()
    collection.total = 7
    assert VectorStore(collection).count() == 7


def test_clear_noops_when_collection_is_empty():
    collection = FakeCollection()
    VectorStore(collection).clear()
    assert collection.calls == [("get", {"include": []})]


def test_clear_deletes_all_returned_ids():
    collection = FakeCollection()
    collection.get_result = {"ids": ["p1:0", "p2:0"]}
    VectorStore(collection).clear()
    assert collection.calls == [
        ("get", {"include": []}),
        ("delete", {"ids": ["p1:0", "p2:0"]}),
    ]


@pytest.mark.parametrize(
    "get_result",
    [None, {}, {"ids": None}, {"ids": "p1:0"}, {"ids": [""]}, {"ids": [1]}],
)
def test_clear_rejects_malformed_get_results(get_result):
    collection = FakeCollection()
    collection.get_result = get_result
    with pytest.raises(ValueError, match="Malformed Chroma get result"):
        VectorStore(collection).clear()
    assert collection.calls == [("get", {"include": []})]


def test_clear_does_not_retry_without_ids_only_include_when_get_raises():
    class BrokenGetCollection(FakeCollection):
        def get(self, **kwargs):
            self.calls.append(("get", kwargs))
            raise TypeError("internal collection failure")

    collection = BrokenGetCollection()
    with pytest.raises(TypeError, match="internal collection failure"):
        VectorStore(collection).clear()
    assert collection.calls == [("get", {"include": []})]


def test_persistent_builds_cpu_cosine_collection_without_real_dependencies(tmp_path, monkeypatch):
    created = {}

    class FakeEmbeddingFunction:
        def __init__(self, **kwargs):
            created["embedding"] = kwargs
            created["embedding_instance"] = self

    class FakeClient:
        def __init__(self, path):
            created["client_path"] = path

        def get_or_create_collection(self, **kwargs):
            created["collection"] = kwargs
            return types.SimpleNamespace(metadata=kwargs["metadata"])

        def close(self):
            created["client_closed"] = created.get("client_closed", 0) + 1

    chromadb = types.ModuleType("chromadb")
    chromadb.PersistentClient = FakeClient
    embedding_functions = types.ModuleType("chromadb.utils.embedding_functions")
    embedding_functions.SentenceTransformerEmbeddingFunction = FakeEmbeddingFunction
    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(sys.modules, "chromadb.utils.embedding_functions", embedding_functions)

    store = VectorStore.persistent(tmp_path / "db", "模型")

    assert (tmp_path / "db").is_dir()
    assert created["client_path"] == str(tmp_path / "db")
    assert created["embedding"] == {
        "model_name": "模型",
        "device": "cpu",
        "normalize_embeddings": True,
    }
    assert created["collection"] == {
        "name": "x_posts",
        "embedding_function": created["embedding_instance"],
        "metadata": {"hnsw:space": "cosine", "xrag:embedding_model": "模型"},
    }

    store.close()
    store.close()
    assert created["client_closed"] == 1


def test_close_is_idempotent_and_injected_collection_needs_no_client():
    VectorStore(FakeCollection()).close()

    class Client:
        def __init__(self) -> None:
            self.closed = 0

        def close(self) -> None:
            self.closed += 1

    client = Client()
    store = VectorStore(FakeCollection(), client=client)

    store.close()
    store.close()

    assert client.closed == 1


def test_close_failure_can_be_retried():
    class FailOnceClient:
        def __init__(self) -> None:
            self.attempts = 0

        def close(self) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("still busy")

    client = FailOnceClient()
    store = VectorStore(FakeCollection(), client=client)

    with pytest.raises(OSError, match="still busy"):
        store.close()
    store.close()
    store.close()

    assert client.attempts == 2


def test_persistent_wraps_dependency_failure(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(RuntimeError, match="initialize persistent Chroma vector store"):
        VectorStore.persistent(tmp_path / "db", "model")


def test_persistent_wraps_and_chains_initialization_failure(tmp_path, monkeypatch):
    cause = OSError("model unavailable")

    class BrokenEmbeddingFunction:
        def __init__(self, **kwargs):
            raise cause

    chromadb = types.ModuleType("chromadb")
    embedding_functions = types.ModuleType("chromadb.utils.embedding_functions")
    embedding_functions.SentenceTransformerEmbeddingFunction = BrokenEmbeddingFunction
    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(sys.modules, "chromadb.utils.embedding_functions", embedding_functions)

    with pytest.raises(
        RuntimeError, match="initialize persistent Chroma vector store"
    ) as exc_info:
        VectorStore.persistent(tmp_path / "db", "broken-model")
    assert exc_info.value.__cause__ is cause


def test_persistent_rejects_collection_bound_to_different_model(tmp_path, monkeypatch):
    closed = 0

    class FakeEmbeddingFunction:
        def __init__(self, **kwargs):
            pass

    class FakeClient:
        def __init__(self, path):
            pass

        def get_or_create_collection(self, **kwargs):
            return types.SimpleNamespace(
                metadata={"hnsw:space": "cosine", "xrag:embedding_model": "old-model"}
            )

        def close(self):
            nonlocal closed
            closed += 1

    chromadb = types.ModuleType("chromadb")
    chromadb.PersistentClient = FakeClient
    embedding_functions = types.ModuleType("chromadb.utils.embedding_functions")
    embedding_functions.SentenceTransformerEmbeddingFunction = FakeEmbeddingFunction
    monkeypatch.setitem(sys.modules, "chromadb", chromadb)
    monkeypatch.setitem(sys.modules, "chromadb.utils.embedding_functions", embedding_functions)

    with pytest.raises(RuntimeError, match="rebuild/reindex required"):
        VectorStore.persistent(tmp_path / "db", "new-model")
    assert closed == 1
