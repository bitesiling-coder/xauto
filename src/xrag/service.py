from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from .config import AppConfig
from .importers import load_posts
from .locking import writer_lock
from .markdown_store import MarkdownStore


_IMPORT_EXTENSIONS = {".yaml", ".yml", ".json", ".md"}
_SECRET = re.compile(r"(?i)\b(auth_token|ct0)\s*[=:]\s*([^\s,;]+)")
_MAX_ERROR_LENGTH = 500


class XragService:
    def __init__(
        self,
        config: AppConfig,
        opencli: Any,
        markdown: MarkdownStore,
        vectors: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], object] | None = None,
    ) -> None:
        self.config = config
        self.opencli = opencli
        self.markdown = markdown
        self.vectors = vectors
        self._sleep = sleep
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def collect(self, keyword: str, limit: int | None = None) -> dict[str, int]:
        effective_limit = self.config.limit_per_keyword if limit is None else limit
        posts = self.opencli.search(keyword, effective_limit)
        counts = {"found": len(posts), "stored": 0, "chunks": 0, "errors": 0}
        with writer_lock(self.config.root):
            for item in posts:
                try:
                    path = self.markdown.upsert(item)
                    counts["stored"] += 1
                    counts["chunks"] += self.vectors.index_post(item, path)
                except Exception as error:
                    counts["errors"] += 1
                    self._log_error(
                        "collect", self._post_id(item), error, sensitive=self._post_text(item)
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
            for path in files:
                try:
                    # Validate and normalize the complete file before writing any post from it.
                    posts = load_posts(path)
                except Exception as error:
                    counts["errors"] += 1
                    self._log_error("import", str(path), error)
                    continue
                for item in posts:
                    try:
                        markdown_path = self.markdown.upsert(item)
                        counts["imported"] += 1
                        counts["chunks"] += self.vectors.index_post(item, markdown_path)
                    except Exception as error:
                        counts["errors"] += 1
                        self._log_error(
                            "import", f"{path}#{self._post_id(item)}", error,
                            sensitive=self._post_text(item),
                        )
        self._write_last_run("import", counts)
        return counts

    def search(self, query: str, top: int) -> Any:
        return self.vectors.search(query, top)

    def rebuild(self) -> dict[str, int]:
        counts = {"documents": 0, "chunks": 0, "errors": 0}
        with writer_lock(self.config.root):
            self.vectors.clear()
            for path, item in self.markdown.iter_posts():
                counts["documents"] += 1
                try:
                    counts["chunks"] += self.vectors.index_post(item, path)
                except Exception as error:
                    counts["errors"] += 1
                    self._log_error(
                        "rebuild", self._post_id(item), error, sensitive=self._post_text(item)
                    )
        self._write_last_run("rebuild", counts)
        return counts

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "documents": sum(1 for _ in self.markdown.iter_posts()),
            "chunks": self.vectors.count(),
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
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                result["last_run_status"] = f"error reading last-run.json: {error}"
        return result

    def _import_files(self, source: Path) -> list[Path]:
        if not source.exists():
            raise ValueError(f"Import path does not exist: {source}")
        if source.is_file():
            if source.suffix.lower() not in _IMPORT_EXTENSIONS:
                raise ValueError(f"Unsupported import file type: {source.suffix or '(no extension)'}")
            return [source]
        if not source.is_dir():
            raise ValueError(f"Import path is not a file or directory: {source}")

        canonical = self.config.markdown_dir.resolve()
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
        message = _redact(str(error))
        if sensitive:
            message = message.replace(sensitive, "[REDACTED]")
        record = {
            "operation": operation,
            "source": _redact(source)[:_MAX_ERROR_LENGTH],
            "error": error.__class__.__name__,
            "message": message[:_MAX_ERROR_LENGTH],
        }
        path = self.config.log_dir / "errors.jsonl"
        try:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
        except (OSError, UnicodeError):
            existing = ""
        self._write_atomic(path, existing + _json_line(record))

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
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
