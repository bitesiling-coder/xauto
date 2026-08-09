from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Any
import uuid

from .config import AppConfig
from .importers import load_posts
from .locking import writer_lock
from .markdown_store import MarkdownStore


_IMPORT_EXTENSIONS = {".yaml", ".yml", ".json", ".md"}
_SECRET = re.compile(
    r'''(?ix)
    ["']?\b(
        (?:(?:twitter|x)[_-])?
        (?:auth[_-]?token|ct0|api[_-]?key|password|passwd|client[_-]?secret|
           access[_-]?token|refresh[_-]?token|authorization)
    )\b["']?
    \s*[:=]\s*
    (?:"[^"]*"|'[^']*'|[^\s,;}]+)
    '''
)
_AUTHORIZATION = re.compile(
    r'''(?ix)
    ["']?\bauthorization\b["']?\s*[:=]\s*
    (?:"[^"]*"|'[^']*'|Digest\b[^\r\n;]*|[^\r\n,;]*)
    '''
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;}\]]+")
_MAX_ERROR_LENGTH = 500
_STAGING_PREFIX = ".xrag-chroma-staging-"
_BACKUP_PREFIX = ".xrag-chroma-backup-"


class XragService:
    def __init__(
        self,
        config: AppConfig,
        opencli: Any,
        markdown: MarkdownStore,
        vectors: Any,
        *,
        vector_factory: Callable[[Path], Any] | None = None,
        rebuild_factory: Callable[[Path], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], object] | None = None,
    ) -> None:
        self.config = config
        self.opencli = opencli
        self.markdown = markdown
        self.vectors = vectors
        self._vector_factory = vector_factory
        self._rebuild_factory = rebuild_factory
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, keyword: str, limit: int | None = None) -> dict[str, int]:
        effective_limit = self.config.limit_per_keyword if limit is None else limit
        posts = self.opencli.search(keyword, effective_limit)
        counts = {"found": len(posts), "stored": 0, "chunks": 0, "errors": 0}
        with writer_lock(self.config.root):
            with self._vector_session() as vectors:
                for item in posts:
                    try:
                        path = self.markdown.upsert(item)
                        counts["stored"] += 1
                        counts["chunks"] += vectors.index_post(item, path)
                    except Exception as error:
                        counts["errors"] += 1
                        self._log_error(
                            "collect",
                            self._post_id(item),
                            error,
                            sensitive=self._post_text(item),
                        )
                self._write_last_run("collect", counts, keyword=keyword)
        return counts

    def collect_all(self) -> list[tuple[str, dict[str, int]]]:
        results: list[tuple[str, dict[str, int]]] = []
        for index, keyword in enumerate(self.config.keywords):
            results.append((keyword, self.collect(keyword)))
            if index + 1 < len(self.config.keywords):
                self._sleep(self.config.delay_seconds)
        return results

    def import_path(self, source: Path) -> dict[str, int]:
        source = Path(source)
        files = self._import_files(source)
        counts = {"files": len(files), "imported": 0, "chunks": 0, "errors": 0}
        with writer_lock(self.config.root):
            with self._vector_session() as vectors:
                for path in files:
                    try:
                        # Validate the complete file before writing any post from it.
                        posts = load_posts(path)
                    except Exception as error:
                        counts["errors"] += 1
                        self._log_error("import", str(path), error)
                        continue
                    for item in posts:
                        try:
                            markdown_path = self.markdown.upsert(item)
                            counts["imported"] += 1
                            counts["chunks"] += vectors.index_post(item, markdown_path)
                        except Exception as error:
                            counts["errors"] += 1
                            self._log_error(
                                "import",
                                f"{path}#{self._post_id(item)}",
                                error,
                                sensitive=self._post_text(item),
                            )
                self._write_last_run("import", counts)
        return counts

    def search(self, query: str, top: int) -> Any:
        with writer_lock(self.config.root):
            with self._vector_session() as vectors:
                return vectors.search(query, top)

    def rebuild(self) -> dict[str, int]:
        if self._rebuild_factory is None:
            raise RuntimeError("Atomic rebuild requires a rebuild factory")
        with writer_lock(self.config.root):
            return self._rebuild_atomic()

    def _rebuild_atomic(self) -> dict[str, int]:
        counts = {"documents": 0, "chunks": 0, "errors": 0}
        stable = self.config.chroma_dir
        parent = stable.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging: Path | None = None
        staging_store: Any | None = None

        try:
            staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=parent))
            self._validate_rebuild_path(staging, parent, _STAGING_PREFIX)
            assert self._rebuild_factory is not None
            staging_store = self._rebuild_factory(staging)

            for path in sorted(self.markdown.directory.glob("*.md"), key=str):
                canonical_path = path.resolve()
                counts["documents"] += 1
                item: object | None = None
                try:
                    item = self.markdown.read(canonical_path)
                    counts["chunks"] += staging_store.index_post(item, canonical_path)
                except Exception as error:
                    counts["errors"] += 1
                    self._log_error(
                        "rebuild",
                        str(canonical_path),
                        error,
                        sensitive=self._post_text(item),
                    )

            if counts["errors"]:
                self._close_vector_store(staging_store)
                staging_store = None
                self._remove_rebuild_path(staging, parent, _STAGING_PREFIX)
                staging = None
                self._write_last_run("rebuild", counts)
                return counts

            actual_chunks = staging_store.count()
            if actual_chunks != counts["chunks"]:
                raise RuntimeError(
                    "Staging Chroma count validation failed: "
                    f"expected {counts['chunks']}, found {actual_chunks}"
                )

            store_to_close = staging_store
            staging_store = None
            self._close_vector_store(store_to_close)
            cleanup_pending = self._swap_rebuild(staging, stable, parent)
            staging = None
            if cleanup_pending is None:
                self._write_last_run("rebuild", counts)
            else:
                self._write_last_run(
                    "rebuild", counts, cleanup_pending=cleanup_pending.name
                )
            return counts
        except Exception as error:
            counts["errors"] += 1
            self._log_error("rebuild", str(staging or stable), error)
            if staging_store is not None:
                try:
                    self._close_vector_store(staging_store)
                except Exception as close_error:
                    self._log_error("rebuild", str(staging or stable), close_error)
            if staging is not None:
                try:
                    self._remove_rebuild_path(staging, parent, _STAGING_PREFIX)
                except Exception as cleanup_error:
                    self._log_error("rebuild", str(staging), cleanup_error)
            self._write_last_run("rebuild", counts)
            raise

    @staticmethod
    def _close_vector_store(store: Any) -> None:
        close = getattr(store, "close", None)
        if callable(close):
            close()

    def _swap_rebuild(
        self, staging: Path, stable: Path, parent: Path
    ) -> Path | None:
        self._validate_rebuild_path(staging, parent, _STAGING_PREFIX)
        backup: Path | None = None
        if stable.exists():
            backup = self._unused_rebuild_path(parent, _BACKUP_PREFIX)
            os.replace(stable, backup)
        try:
            os.replace(staging, stable)
        except Exception as swap_error:
            if backup is not None:
                try:
                    os.replace(backup, stable)
                    backup = None
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"Chroma swap failed and rollback failed; old index retained at {backup}"
                    ) from rollback_error
            raise swap_error
        if backup is not None:
            try:
                self._remove_rebuild_path(backup, parent, _BACKUP_PREFIX)
            except Exception as cleanup_error:
                self._log_error(
                    "rebuild-cleanup", backup.name, cleanup_error
                )
                return backup
        return None

    @staticmethod
    def _unused_rebuild_path(parent: Path, prefix: str) -> Path:
        for _ in range(10):
            candidate = parent / f"{prefix}{uuid.uuid4().hex}"
            if not candidate.exists():
                return candidate
        raise RuntimeError("Could not allocate a unique Chroma rebuild path")

    @staticmethod
    def _validate_rebuild_path(path: Path, parent: Path, prefix: str) -> None:
        if path.parent.resolve() != parent.resolve() or not path.name.startswith(prefix):
            raise RuntimeError(f"Refusing unverified Chroma rebuild path: {path}")
        if path.name == prefix:
            raise RuntimeError(f"Refusing incomplete Chroma rebuild path: {path}")

    @classmethod
    def _remove_rebuild_path(cls, path: Path, parent: Path, prefix: str) -> None:
        cls._validate_rebuild_path(path, parent, prefix)
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_dir():
            path.unlink()
            return
        shutil.rmtree(path)

    def status(self) -> dict[str, Any]:
        with writer_lock(self.config.root):
            with self._vector_session() as vectors:
                paths = sorted(self.markdown.directory.glob("*.md"), key=str)
                document_errors = 0
                for path in paths:
                    try:
                        self.markdown.read(path)
                    except Exception:
                        document_errors += 1
                result: dict[str, Any] = {
                    "documents": len(paths),
                    "document_errors": document_errors,
                    "chunks": vectors.count(),
                    "keywords": len(self.config.keywords),
                    "last_run": None,
                }
                path = self.config.log_dir / "last-run.json"
                if path.exists():
                    try:
                        value = json.loads(path.read_text(encoding="utf-8"))
                        if not isinstance(value, dict):
                            raise ValueError("last-run root is not an object")
                        result["last_run"] = value
                    except (
                        OSError,
                        UnicodeError,
                        json.JSONDecodeError,
                        ValueError,
                    ) as error:
                        result["last_run_status"] = (
                            f"error reading last-run.json: {error}"
                        )
                return result

    @contextmanager
    def _vector_session(self) -> Iterator[Any]:
        if self._vector_factory is None:
            if self.vectors is None:
                raise RuntimeError("Vector store requires a vector factory")
            yield self.vectors
            return

        store = self._vector_factory(self.config.chroma_dir)
        try:
            yield store
        finally:
            self._close_vector_store(store)

    def _import_files(self, source: Path) -> list[Path]:
        if not source.exists():
            raise ValueError(f"Import path does not exist: {source}")
        canonical = self.markdown.directory.resolve()
        if source.is_file():
            if source.suffix.lower() not in _IMPORT_EXTENSIONS:
                raise ValueError(f"Unsupported import file type: {source.suffix or '(no extension)'}")
            if _is_within(source, canonical):
                raise ValueError(f"Cannot import a canonical Markdown file: {source}")
            return [source]
        if not source.is_dir():
            raise ValueError(f"Import path is not a file or directory: {source}")

        files = [
            path
            for path in source.rglob("*")
            if path.is_file()
            and path.suffix.lower() in _IMPORT_EXTENSIONS
            and not _is_within(path, canonical)
        ]
        return sorted(files, key=str)

    def _write_last_run(self, operation: str, counts: dict[str, int], **context: str) -> None:
        record: dict[str, object] = {
            "operation": operation,
            "time": self._now(),
            **context,
            "counts": counts,
        }
        self._write_atomic(self.config.log_dir / "last-run.json", _json_line(record))

    def _log_error(
        self, operation: str, source: str, error: Exception, *, sensitive: str = ""
    ) -> None:
        raw_message = str(error)
        message = (
            "post processing failed"
            if sensitive
            else _redact(raw_message.splitlines()[0] if raw_message else "")
        )
        record = {
            "operation": operation,
            "source": _redact(source)[:_MAX_ERROR_LENGTH],
            "error": error.__class__.__name__,
            "message": message[:_MAX_ERROR_LENGTH],
        }
        path = self.config.log_dir / "errors.jsonl"
        try:
            self._append_error(path, _json_line(record))
        except Exception:
            pass

    @staticmethod
    def _append_error(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open(mode="a", encoding="utf-8", newline="\n") as log_file:
            log_file.write(line)
            log_file.flush()
            os.fsync(log_file.fileno())

    def _now(self) -> str:
        value = self._clock()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        return str(value)

    @staticmethod
    def _post_id(item: object) -> str:
        value = getattr(item, "id", "unknown")
        return str(value)[:_MAX_ERROR_LENGTH]

    @staticmethod
    def _post_text(item: object) -> str:
        value = getattr(item, "text", "")
        return value if isinstance(value, str) else ""

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory)
        return True
    except ValueError:
        return False


def _json_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _redact(value: str) -> str:
    redacted = _AUTHORIZATION.sub("authorization=[REDACTED]", value)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
