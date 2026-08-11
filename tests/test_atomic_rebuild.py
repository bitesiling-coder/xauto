from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.models import Post
import xrag.service as service_module
from xrag.service import XragService
from xrag.translation import TranslationFailure, TranslationOutcome


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


class ReuseTranslation:
    def __init__(self) -> None:
        self.preflight_calls = 0
        self.calls: list[tuple[Post, Post | None]] = []

    def preflight(self) -> None:
        self.preflight_calls += 1

    def enrich(self, item: Post, existing: Post | None) -> TranslationOutcome:
        self.calls.append((item, existing))
        return TranslationOutcome(item, 0, 1, 0, ())


class UpdatingTranslation:
    def preflight(self) -> None:
        pass

    def enrich(self, item: Post, existing: Post | None) -> TranslationOutcome:
        return TranslationOutcome(replace(item, source_type="translated"), 1, 0, 0, ())


class FailingTranslation:
    def preflight(self) -> None:
        pass

    def enrich(self, item: Post, existing: Post | None) -> TranslationOutcome:
        return TranslationOutcome(
            item, 0, 0, 0, (TranslationFailure("post", "translation failed"),)
        )


class UnavailableTranslation:
    def preflight(self) -> None:
        raise RuntimeError("translation model secret source")


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


def test_translate_all_reuses_existing_posts_rebuilds_once_and_preserves_media(
    tmp_path: Path,
) -> None:
    stores: list[StagingStore] = []

    def factory(path: Path) -> StagingStore:
        store = StagingStore(path)
        stores.append(store)
        return store

    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")
    media = tmp_path / "data/media/sentinel.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"do not alter")
    translation = ReuseTranslation()
    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=factory, translation=translation,
    )

    result = service.translate_all()

    assert result == {
        "scanned": 1,
        "translated": 0,
        "reused": 1,
        "skipped": 0,
        "errors": 0,
        "translation_errors": 0,
        "updated_documents": 0,
        "missing_source_files": 0,
        "chunks": 2,
    }
    assert translation.preflight_calls == 1
    assert translation.calls[0][0] == translation.calls[0][1]
    assert len(stores) == 1
    assert media.read_bytes() == b"do not alter"
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run["operation"] == "translation-backfill"
    assert last_run["counts"] == result


def test_translate_all_counts_translation_failures_separately(tmp_path: Path) -> None:
    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")
    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=StagingStore, translation=FailingTranslation(),
    )

    result = service.translate_all()

    assert result == {
        "scanned": 1,
        "translated": 0,
        "reused": 0,
        "skipped": 0,
        "errors": 1,
        "translation_errors": 1,
        "updated_documents": 0,
        "missing_source_files": 0,
        "chunks": 2,
    }


def test_translate_all_preflight_failure_counts_translation_error(tmp_path: Path) -> None:
    service = XragService(
        configuration(tmp_path), object(), MarkdownStore(tmp_path / "data/markdown"), None,
        rebuild_factory=StagingStore, translation=UnavailableTranslation(),
    )

    with pytest.raises(RuntimeError, match="translation unavailable"):
        service.translate_all()

    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run["counts"] == {
        "scanned": 0,
        "translated": 0,
        "reused": 0,
        "skipped": 0,
        "errors": 1,
        "translation_errors": 1,
        "updated_documents": 0,
        "missing_source_files": 0,
        "chunks": 0,
    }


def test_translate_all_rebuild_exception_records_safe_backfill_result(
    tmp_path: Path,
) -> None:
    stable = tmp_path / "data/chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")

    class WrongCountStore(StagingStore):
        def count(self) -> int:
            return super().count() - 1

    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")
    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=WrongCountStore, translation=UpdatingTranslation(),
    )

    with pytest.raises(RuntimeError, match="^translation index rebuild failed$"):
        service.translate_all()

    result = {
        "scanned": 1,
        "translated": 1,
        "reused": 0,
        "skipped": 0,
        "errors": 1,
        "translation_errors": 0,
        "updated_documents": 1,
        "missing_source_files": 0,
        "chunks": 0,
    }
    assert (stable / "old-sentinel").read_bytes() == b"old"
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run == {
        "operation": "translation-backfill",
        "time": last_run["time"],
        "outcome": "failed",
        "counts": result,
    }
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert str(tmp_path) not in errors


def test_translate_all_permits_hash_from_its_own_markdown_upsert(tmp_path: Path) -> None:
    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")
    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=StagingStore, translation=UpdatingTranslation(),
    )

    result = service.translate_all()

    assert result["updated_documents"] == 1
    assert markdown.get("a").source_type == "translated"


def test_translate_all_rejects_unexpected_markdown_byte_change(tmp_path: Path) -> None:
    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")

    class MutatingTranslation:
        def preflight(self) -> None:
            pass

        def enrich(self, item: Post, existing: Post | None) -> TranslationOutcome:
            path = markdown.directory / "a.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace("body a", "tampered"),
                encoding="utf-8",
            )
            return TranslationOutcome(item, 0, 1, 0, ())

    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=StagingStore, translation=MutatingTranslation(),
    )

    with pytest.raises(RuntimeError, match="^translation backfill removed source data$"):
        service.translate_all()


def test_translate_all_rejects_unexpected_media_byte_change(tmp_path: Path) -> None:
    markdown = MarkdownStore(tmp_path / "data/markdown")
    add_post(markdown, "a")
    media = tmp_path / "data/media/sentinel.jpg"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"original")

    class MutatingMediaTranslation:
        def preflight(self) -> None:
            pass

        def enrich(self, item: Post, existing: Post | None) -> TranslationOutcome:
            media.write_bytes(b"tampered")
            return TranslationOutcome(item, 0, 1, 0, ())

    service = XragService(
        configuration(tmp_path), object(), markdown, None,
        rebuild_factory=StagingStore, translation=MutatingMediaTranslation(),
    )

    with pytest.raises(RuntimeError, match="^translation backfill removed source data$"):
        service.translate_all()


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
    errors = (tmp_path / "logs" / "errors.jsonl").read_text(encoding="utf-8")
    assert "rebuild failed" in errors
    assert "model unavailable" not in errors
    assert str(tmp_path) not in errors


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


def test_backup_cleanup_failure_keeps_new_index_and_records_success_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stable = tmp_path / "data" / "chroma"
    stable.mkdir(parents=True)
    (stable / "old-sentinel").write_bytes(b"old")
    service, markdown = service_with_factory(tmp_path, StagingStore)
    add_post(markdown, "a")
    real_rmtree = service_module.shutil.rmtree

    def fail_backup_cleanup(path: object) -> None:
        if Path(path).name.startswith(".xrag-chroma-backup-"):
            raise OSError("cleanup blocked auth_token=never-log-this")
        real_rmtree(path)

    monkeypatch.setattr(service_module.shutil, "rmtree", fail_backup_cleanup)

    result = service.rebuild()

    assert result == {"documents": 1, "chunks": 2, "errors": 0}
    assert (stable / "new-sentinel").exists()
    assert not (stable / "old-sentinel").exists()
    backups = list(stable.parent.glob(".xrag-chroma-backup-*"))
    assert len(backups) == 1 and (backups[0] / "old-sentinel").exists()
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run["counts"] == result
    assert last_run["cleanup_pending"] == backups[0].name
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "rebuild-cleanup" in errors
    assert "cleanup blocked" in errors
    assert "never-log-this" not in errors
