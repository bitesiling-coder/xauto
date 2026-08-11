from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
import time

import pytest

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.models import Post
import xrag.service as service_module
from xrag.service import XragService


def configuration(root: Path) -> AppConfig:
    return AppConfig(root, False, "03:00", "UTC", 7, 0, ("AI",), "model")


def test_collect_searches_remotely_before_lock_and_uses_vectors_only_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    lock_active = False

    @contextmanager
    def tracking_lock(root: Path):
        nonlocal lock_active
        events.append("lock-enter")
        lock_active = True
        try:
            yield
        finally:
            events.append("lock-exit")
            lock_active = False

    class OpenCLI:
        def search(self, keyword: str, limit: int) -> list[Post]:
            assert not lock_active
            events.append("remote-search")
            return [Post("one", "Ada", "body", "2026-08-09T00:00:00Z", "url")]

    class Store:
        def index_post(self, item: Post, path: Path) -> int:
            assert lock_active
            events.append("index")
            return 1

        def close(self) -> None:
            assert lock_active
            events.append("close")

    def factory(path: Path) -> Store:
        assert lock_active
        assert path == tmp_path / "data/chroma"
        events.append("factory")
        return Store()

    monkeypatch.setattr(service_module, "writer_lock", tracking_lock)
    service = XragService(
        configuration(tmp_path),
        OpenCLI(),
        MarkdownStore(tmp_path / "data/markdown"),
        None,
        vector_factory=factory,
    )

    assert service.collect("AI") == {
        "found": 1,
        "stored": 1,
        "chunks": 1,
        "errors": 0,
        "translated": 0,
        "translation_reused": 0,
        "translation_skipped": 0,
        "translation_errors": 0,
    }
    assert events == [
        "remote-search",
        "lock-enter",
        "factory",
        "index",
        "close",
        "lock-exit",
    ]


def test_import_factory_and_close_are_inside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.json"
    source.write_text('[{"id":"one","text":"body"}]', encoding="utf-8")
    lock_active = False
    states: list[tuple[str, bool]] = []

    @contextmanager
    def tracking_lock(root: Path):
        nonlocal lock_active
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    class Store:
        def index_post(self, item: Post, path: Path) -> int:
            states.append(("index", lock_active))
            return 1

        def close(self) -> None:
            states.append(("close", lock_active))

    def factory(path: Path) -> Store:
        states.append(("factory", lock_active))
        return Store()

    monkeypatch.setattr(service_module, "writer_lock", tracking_lock)
    service = XragService(
        configuration(tmp_path),
        object(),
        MarkdownStore(tmp_path / "data/markdown"),
        None,
        vector_factory=factory,
    )

    assert service.import_path(source)["errors"] == 0
    assert states == [("factory", True), ("index", True), ("close", True)]


@pytest.mark.parametrize("operation", ["collect", "search", "status"])
def test_vector_close_precedes_lock_release_when_operation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    events: list[str] = []

    @contextmanager
    def tracking_lock(root: Path):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    class OpenCLI:
        def search(self, keyword: str, limit: int) -> list[Post]:
            return [Post("one", "Ada", "body", "2026-08-09T00:00:00Z", "url")]

    class Store:
        def index_post(self, item: Post, path: Path) -> int:
            events.append("index-error")
            raise RuntimeError("index failed")

        def search(self, query: str, top: int) -> object:
            events.append("query-error")
            raise RuntimeError("query failed")

        def count(self) -> int:
            events.append("count-error")
            raise RuntimeError("count failed")

        def close(self) -> None:
            events.append("close")

    def factory(path: Path) -> Store:
        events.append("factory")
        return Store()

    monkeypatch.setattr(service_module, "writer_lock", tracking_lock)
    service = XragService(
        configuration(tmp_path),
        OpenCLI(),
        MarkdownStore(tmp_path / "data/markdown"),
        None,
        vector_factory=factory,
    )

    if operation == "collect":
        assert service.collect("AI")["errors"] == 1
    else:
        with pytest.raises(RuntimeError, match=f"{'query' if operation == 'search' else 'count'} failed"):
            getattr(service, operation)(*(('query', 1) if operation == "search" else ()))

    assert events[-2:] == ["close", "lock-exit"]


def test_search_waits_for_rebuild_lock_before_opening_stable_store(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    markdown = MarkdownStore(config.markdown_dir)
    staging_ready = threading.Event()
    allow_swap = threading.Event()
    search_started = threading.Event()
    stable_opened = threading.Event()
    errors: list[BaseException] = []
    results: list[object] = []

    class RebuildStore:
        def __init__(self, path: Path) -> None:
            (path / "new-sentinel").write_text("new", encoding="utf-8")

        def count(self) -> int:
            staging_ready.set()
            assert allow_swap.wait(2)
            return 0

        def close(self) -> None:
            pass

    class SearchStore:
        def search(self, query: str, top: int) -> list[str]:
            return ["found"]

        def close(self) -> None:
            pass

    def stable_factory(path: Path) -> SearchStore:
        assert path == config.chroma_dir
        assert (path / "new-sentinel").exists()
        stable_opened.set()
        return SearchStore()

    rebuild_service = XragService(
        config,
        object(),
        markdown,
        None,
        rebuild_factory=RebuildStore,
    )
    search_service = XragService(
        config,
        object(),
        markdown,
        None,
        vector_factory=stable_factory,
    )

    def run_rebuild() -> None:
        try:
            rebuild_service.rebuild()
        except BaseException as error:
            errors.append(error)

    def run_search() -> None:
        search_started.set()
        try:
            results.append(search_service.search("query", 1))
        except BaseException as error:
            errors.append(error)

    rebuild_thread = threading.Thread(target=run_rebuild)
    search_thread = threading.Thread(target=run_search)
    rebuild_thread.start()
    assert staging_ready.wait(2)
    assert not config.chroma_dir.exists()
    search_thread.start()
    assert search_started.wait(1)
    time.sleep(0.1)
    assert not stable_opened.is_set()
    assert not config.chroma_dir.exists()

    allow_swap.set()
    rebuild_thread.join(2)
    search_thread.join(2)

    assert not errors
    assert results == [["found"]]
    assert stable_opened.is_set()
