from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.models import Post
import xrag.service as service_module
from xrag.service import XragService


def configuration(root: Path) -> AppConfig:
    return AppConfig(root, False, "03:00", "UTC", 7, 0, ("AI",), "model")


def add_post(markdown: MarkdownStore, post_id: str) -> Path:
    return markdown.upsert(
        Post(post_id, "Ada", f"body {post_id}", "2026-08-09T00:00:00Z", f"https://x.com/{post_id}")
    )


def service_with_factory(
    tmp_path: Path, factory: object
) -> tuple[XragService, MarkdownStore]:
    markdown = MarkdownStore(tmp_path / "data" / "markdown")
    service = XragService(
        configuration(tmp_path), object(), markdown, None, rebuild_factory=factory
    )
    return service, markdown


class StagingStore:
    def __init__(self, path: Path, *, fail_on: set[str] | None = None) -> None:
        self.path = path
        self.fail_on = fail_on or set()
        self.indexed: list[tuple[str, Path]] = []
        self.closed = False
        (path / "new-sentinel").write_text("fresh index", encoding="utf-8")

    def index_post(self, item: Post, path: Path) -> int:
        self.indexed.append((item.id, path))
        if item.id in self.fail_on:
            raise RuntimeError("index failed")
        return 2

    def count(self) -> int:
        return sum(2 for post_id, _ in self.indexed if post_id not in self.fail_on)

    def close(self) -> None:
        self.closed = True


def sibling_workdirs(stable: Path) -> list[Path]:
    return sorted(stable.parent.glob(".xrag-chroma-*"), key=str)


def test_atomic_rebuild_replaces_unopened_corrupt_old_directory(tmp_path: Path) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "corrupt-model-sentinel").write_bytes(b"old bytes")
    stores: list[StagingStore] = []
    factory_paths: list[Path] = []

    def factory(path: Path) -> StagingStore:
        factory_paths.append(path)
        assert path != stable
        store = StagingStore(path)
        stores.append(store)
        return store

    service, markdown = service_with_factory(tmp_path, factory)
    add_post(markdown, "b")
    add_post(markdown, "a")

    result = service.rebuild()

    assert result == {"documents": 2, "chunks": 4, "errors": 0}
    assert not (stable / "corrupt-model-sentinel").exists()
    assert (stable / "new-sentinel").read_text(encoding="utf-8") == "fresh index"
    assert stores[0].closed
    assert [item[0] for item in stores[0].indexed] == ["a", "b"]
    assert all(path.is_absolute() for _, path in stores[0].indexed)
    assert sibling_workdirs(stable) == []


@pytest.mark.parametrize("failure", ["malformed", "index"])
def test_document_failure_preserves_old_and_cleans_staging(
    tmp_path: Path, failure: str
) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"preserve exactly")
    stores: list[StagingStore] = []

    def factory(path: Path) -> StagingStore:
        store = StagingStore(path, fail_on={"b"} if failure == "index" else set())
        stores.append(store)
        return store

    service, markdown = service_with_factory(tmp_path, factory)
    add_post(markdown, "a")
    if failure == "malformed":
        (markdown.directory / "b.md").write_text("not front matter\n", encoding="utf-8")
    else:
        add_post(markdown, "b")
    add_post(markdown, "c")

    result = service.rebuild()

    assert result == {"documents": 3, "chunks": 4, "errors": 1}
    assert (stable / "old-sentinel").read_bytes() == b"preserve exactly"
    assert stores[0].closed
    assert [post_id for post_id, _ in stores[0].indexed] == ["a", "b", "c"] if failure == "index" else ["a", "c"]
    assert sibling_workdirs(stable) == []
    assert json.loads((tmp_path / "logs" / "last-run.json").read_text(encoding="utf-8"))["counts"] == result


def test_factory_failure_preserves_old_cleans_staging_and_records_failure(tmp_path: Path) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")

    def factory(path: Path) -> StagingStore:
        (path / "partial").write_text("partial", encoding="utf-8")
        raise RuntimeError("model unavailable")

    service, markdown = service_with_factory(tmp_path, factory)
    add_post(markdown, "a")

    with pytest.raises(RuntimeError, match="model unavailable"):
        service.rebuild()

    assert (stable / "old-sentinel").read_bytes() == b"old"
    assert sibling_workdirs(stable) == []
    last_run = json.loads((tmp_path / "logs" / "last-run.json").read_text(encoding="utf-8"))
    assert last_run["operation"] == "rebuild"
    assert last_run["counts"] == {"documents": 0, "chunks": 0, "errors": 1}
    assert "model unavailable" in (tmp_path / "logs" / "errors.jsonl").read_text(encoding="utf-8")


def test_second_rename_failure_rolls_original_directory_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")
    stores: list[StagingStore] = []

    def factory(path: Path) -> StagingStore:
        store = StagingStore(path)
        stores.append(store)
        return store

    service, markdown = service_with_factory(tmp_path, factory)
    add_post(markdown, "a")
    real_replace = os.replace
    failed = False

    def fail_staging_swap(source: object, destination: object) -> None:
        nonlocal failed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not failed
            and source_path.name.startswith(".xrag-chroma-staging-")
            and destination_path == stable
        ):
            failed = True
            raise OSError("simulated second rename failure")
        real_replace(source, destination)

    monkeypatch.setattr(service_module.os, "replace", fail_staging_swap)

    with pytest.raises(OSError, match="simulated second rename failure"):
        service.rebuild()

    assert (stable / "old-sentinel").read_bytes() == b"old"
    assert stores[0].closed
    assert sibling_workdirs(stable) == []
    last_run = json.loads((tmp_path / "logs" / "last-run.json").read_text(encoding="utf-8"))
    assert last_run["counts"]["errors"] == 1


def test_success_closes_before_swap_removes_backup_and_validates_chunk_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")
    stores: list[StagingStore] = []

    def factory(path: Path) -> StagingStore:
        store = StagingStore(path)
        stores.append(store)
        return store

    service, markdown = service_with_factory(tmp_path, factory)
    add_post(markdown, "a")
    real_replace = os.replace

    def observe_replace(source: object, destination: object) -> None:
        if Path(source).name.startswith(".xrag-chroma-staging-"):
            assert stores[0].closed
        real_replace(source, destination)

    monkeypatch.setattr(service_module.os, "replace", observe_replace)

    assert service.rebuild() == {"documents": 1, "chunks": 2, "errors": 0}
    assert (stable / "new-sentinel").exists()
    assert not (stable / "old-sentinel").exists()
    assert sibling_workdirs(stable) == []


def test_count_mismatch_never_swaps_complete_looking_staging(tmp_path: Path) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")

    class WrongCountStore(StagingStore):
        def count(self) -> int:
            return super().count() - 1

    service, markdown = service_with_factory(tmp_path, WrongCountStore)
    add_post(markdown, "a")

    with pytest.raises(RuntimeError, match="count"):
        service.rebuild()

    assert (stable / "old-sentinel").read_bytes() == b"old"
    assert sibling_workdirs(stable) == []
