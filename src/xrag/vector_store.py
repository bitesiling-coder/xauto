from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from pathlib import Path
from typing import Any

from .chunking import chunk_text
from .models import Post, SearchHit


class VectorStore:
    def __init__(
        self,
        collection: Any,
        *,
        client: Any | None = None,
        max_chars: int = 500,
        overlap: int = 80,
    ) -> None:
        self.collection = collection
        self._client = client
        self._closed = False
        self.max_chars = max_chars
        self.overlap = overlap

    @classmethod
    def persistent(cls, path: Path, model_name: str) -> "VectorStore":
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        client: Any | None = None
        try:
            import chromadb
            from chromadb.utils.embedding_functions import (
                SentenceTransformerEmbeddingFunction,
            )

            embedding_function = SentenceTransformerEmbeddingFunction(
                model_name=model_name,
                device="cpu",
                normalize_embeddings=True,
            )
            client = chromadb.PersistentClient(path=str(path))
            collection = client.get_or_create_collection(
                name="x_posts",
                embedding_function=embedding_function,
                metadata={
                    "hnsw:space": "cosine",
                    "xrag:embedding_model": model_name,
                },
            )
            collection_metadata = collection.metadata
            if (
                not isinstance(collection_metadata, Mapping)
                or collection_metadata.get("xrag:embedding_model") != model_name
            ):
                raise RuntimeError(
                    "Chroma collection embedding model does not match; "
                    "rebuild/reindex required"
                )
        except Exception as exc:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            raise RuntimeError(
                f"Failed to initialize persistent Chroma vector store at {path} "
                f"with model {model_name!r}: {exc}"
            ) from exc
        return cls(collection, client=client)

    def close(self) -> None:
        if self._closed:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self._closed = True

    def index_post(self, post: Post, markdown_path: Path) -> int:
        chunks = chunk_text(post.searchable_text, self.max_chars, self.overlap)
        existing = self.collection.get(
            where={"post_id": post.id},
            include=[],
        )
        existing_ids = _extract_ids(existing)
        if not chunks:
            if existing_ids:
                self.collection.delete(ids=existing_ids)
            return 0

        new_ids = [f"{post.id}:{index}" for index in range(len(chunks))]
        metadata = {
            "post_id": post.id,
            "author": post.author,
            "created_at": post.created_at,
            "url": post.url,
            "markdown_path": str(markdown_path),
        }
        self.collection.upsert(
            ids=new_ids,
            documents=chunks,
            metadatas=[metadata.copy() for _ in chunks],
        )
        new_id_set = set(new_ids)
        stale_ids = [item_id for item_id in existing_ids if item_id not in new_id_set]
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        return len(chunks)

    def search(self, query: str, top: int) -> list[SearchHit]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if top <= 0:
            raise ValueError("top must be greater than zero")

        result = self.collection.query(query_texts=[query], n_results=top)
        try:
            if not isinstance(result, Mapping):
                raise TypeError("result must be a mapping")

            result_sets = []
            for key in ("ids", "documents", "metadatas", "distances"):
                outer = result[key]
                if not _is_sequence(outer) or not outer:
                    raise TypeError(f"{key} must be a non-empty outer sequence")
                inner = outer[0]
                if not _is_sequence(inner):
                    raise TypeError(f"{key}[0] must be a sequence")
                result_sets.append(inner)

            first_ids, documents, metadatas, distances = result_sets
            if not (len(first_ids) == len(documents) == len(metadatas) == len(distances)):
                raise ValueError("result set lengths differ")
            if not first_ids:
                return []

            hits = []
            for document, metadata, distance in zip(documents, metadatas, distances):
                if not isinstance(metadata, dict):
                    raise TypeError("metadata must be a mapping")
                if (
                    isinstance(distance, bool)
                    or not isinstance(distance, (int, float))
                    or not math.isfinite(distance)
                ):
                    raise TypeError("distance must be a finite real number")
                score = round(max(0.0, min(1.0, 1.0 - distance)), 4)
                hits.append(
                    SearchHit(
                        post_id=str(metadata["post_id"]),
                        text=str(document),
                        author=str(metadata["author"]),
                        created_at=str(metadata["created_at"]),
                        url=str(metadata["url"]),
                        score=score,
                        markdown_path=str(metadata["markdown_path"]),
                    )
                )
            return hits
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError(f"Malformed Chroma query result: {exc}") from exc

    def count(self) -> int:
        return self.collection.count()

    def clear(self) -> None:
        result = self.collection.get(include=[])
        ids = _extract_ids(result)
        if ids:
            self.collection.delete(ids=ids)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _extract_ids(result: Any) -> list[str]:
    try:
        if not isinstance(result, Mapping):
            raise TypeError("result must be a mapping")
        ids = result["ids"]
        if not _is_sequence(ids):
            raise TypeError("ids must be a sequence")
        if any(not isinstance(item_id, str) or not item_id for item_id in ids):
            raise TypeError("ids must contain only nonempty strings")
        return list(ids)
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Malformed Chroma get result: {exc}") from exc
