from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import AppConfig
from xrag.markdown_store import MarkdownStore
from xrag.media_store import MediaArchiveResult, MediaFailure, MediaStore
from xrag.models import LocalMedia, Post
from xrag.opencli import OpenCLIClient, OpenCLIError, SearchBatch, SearchRejection
import xrag.service as service_module
from xrag.service import XragService


def post(post_id: str, text: str = "body") -> Post:
    return Post(post_id, "Ada", text, "2026-08-09T00:00:00Z", f"https://x.com/{post_id}")


class OpenCLI:
    def __init__(self, posts: list[Post] | None = None) -> None:
        self.posts = posts or []
        self.calls: list[tuple[str, int]] = []

    def search(self, keyword: str, limit: int) -> list[Post]:
        self.calls.append((keyword, limit))
        return self.posts


class DiagnosticOpenCLI:
    def __init__(self, batch: SearchBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[str, int]] = []

    def search_batch(self, keyword: str, limit: int) -> SearchBatch:
        self.calls.append((keyword, limit))
        return self.batch


class Vectors:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.failures = failures or set()
        self.indexed: list[str] = []
        self.cleared = 0
        self.search_args: tuple[str, int] | None = None

    def index_post(self, item: Post, path: Path) -> int:
        self.indexed.append(item.id)
        if item.id in self.failures:
            raise RuntimeError("embedding rejected snippet RECOGNIZABLE")
        return 2

    def clear(self) -> None:
        self.cleared += 1

    def count(self) -> int:
        return len(self.indexed) * 2

    def search(self, query: str, top: int) -> list[str]:
        self.search_args = (query, top)
        return [query]


class Media:
    def __init__(
        self,
        result: MediaArchiveResult | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.result = result
        self.failure = failure
        self.archived: list[Post] = []

    def archive(self, item: Post) -> MediaArchiveResult:
        self.archived.append(item)
        if self.failure is not None:
            raise self.failure
        return self.result or MediaArchiveResult(item, ())


def config(root: Path, keywords: tuple[str, ...] = ("AI", "GPU")) -> AppConfig:
    return AppConfig(root, False, "03:00", "UTC", 7, 3, keywords, "model")


@contextmanager
def unlocked(root: Path, timeout: float = 1):
    yield


@pytest.fixture(autouse=True)
def no_real_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "writer_lock", unlocked)


def make_service(
    tmp_path: Path,
    client: OpenCLI,
    vectors: Vectors,
    media: object | None = None,
) -> XragService:
    return XragService(
        config(tmp_path), client, MarkdownStore(tmp_path / "data" / "markdown"), vectors,
        media=media,
        clock=lambda: "2026-08-09T12:00:00Z",
    )


def test_collect_archives_before_markdown_and_vectors(tmp_path: Path) -> None:
    original = post("123")
    archived = replace(
        original,
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/image",
                "../media/123/image-01.jpg",
                "image/jpeg",
            ),
        ),
    )
    media = Media(MediaArchiveResult(archived, ()))
    vectors = Vectors()
    service = make_service(tmp_path, OpenCLI([original]), vectors, media)

    result = service.collect("AI")

    assert result == {"found": 1, "stored": 1, "chunks": 2, "errors": 0}
    assert media.archived == [original]
    assert service.markdown.get("123") == archived


def test_media_failure_is_logged_but_text_is_stored(tmp_path: Path) -> None:
    item = post("123", "safe body")
    media = Media(
        MediaArchiveResult(
            item,
            (
                MediaFailure(
                    "post",
                    "image",
                    "https://pbs.twimg.com/media/image",
                    "TimeoutError",
                    "media download failed",
                ),
            ),
        )
    )
    service = make_service(tmp_path, OpenCLI([item]), Vectors(), media)

    result = service.collect("AI")

    assert result == {"found": 1, "stored": 1, "chunks": 2, "errors": 1}
    assert service.markdown.get("123") is not None
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "TimeoutError" in errors
    assert "media download failed" in errors


def test_unexpected_media_exception_does_not_block_text_storage(tmp_path: Path) -> None:
    service = make_service(
        tmp_path,
        OpenCLI([post("123")]),
        Vectors(),
        Media(failure=RuntimeError("RECOGNIZABLE secret media detail")),
    )

    result = service.collect("AI")

    assert result == {"found": 1, "stored": 1, "chunks": 2, "errors": 1}
    assert service.markdown.get("123") is not None
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "media archival failed" in errors
    assert "RECOGNIZABLE" not in errors


def test_recollect_passes_existing_media_mapping_to_archiver(tmp_path: Path) -> None:
    media = Media()
    service = make_service(tmp_path, OpenCLI([post("123")]), Vectors(), media)
    existing = replace(
        post("123"),
        local_media=(
            LocalMedia(
                "post",
                "image",
                "https://pbs.twimg.com/media/image",
                "../media/123/image-01.jpg",
                "image/jpeg",
            ),
        ),
    )
    service.markdown.upsert(existing)

    service.collect("AI")

    assert media.archived[0].local_media == existing.local_media


def test_import_non_x_media_counts_failure_but_keeps_text(tmp_path: Path) -> None:
    source = tmp_path / "import.yaml"
    source.write_text(
        "id: imported\ntext: imported body\nmedia_urls:\n  - https://example.com/image.jpg\n",
        encoding="utf-8",
    )
    media = MediaStore(
        tmp_path / "data/media",
        open_url=lambda request, timeout: (_ for _ in ()).throw(
            AssertionError("non-X URL must not be opened")
        ),
    )
    service = make_service(tmp_path, OpenCLI(), Vectors(), media)

    result = service.import_path(source)

    assert result == {"files": 1, "imported": 1, "chunks": 2, "errors": 1}
    assert service.markdown.get("imported") is not None


def test_collect_stores_before_indexing_continues_after_index_error_and_writes_summary(tmp_path: Path) -> None:
    vectors = Vectors({"bad"})
    service = make_service(
        tmp_path, OpenCLI([post("bad", "RECOGNIZABLE BODY"), post("good")]), vectors
    )

    result = service.collect("AI", limit=2)

    assert result == {"found": 2, "stored": 2, "chunks": 2, "errors": 1}
    assert vectors.indexed == ["bad", "good"]
    assert (tmp_path / "data/markdown/bad.md").exists()
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run == {"operation": "collect", "time": "2026-08-09T12:00:00Z", "keyword": "AI", "counts": result}
    assert list((tmp_path / "logs").glob("*.tmp")) == []
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "bad" in errors and "RuntimeError" in errors and "post processing failed" in errors
    assert "embedding rejected snippet RECOGNIZABLE" not in errors
    assert "RECOGNIZABLE" not in errors


def test_collect_counts_and_logs_rejected_rows_without_storing_or_indexing_them(
    tmp_path: Path,
) -> None:
    client = DiagnosticOpenCLI(
        SearchBatch(
            posts=(post("valid"),),
            rejections=(
                SearchRejection(1, "rejected-id", "missing or blank text"),
            ),
        )
    )
    vectors = Vectors()
    service = XragService(
        config(tmp_path), client, MarkdownStore(tmp_path / "data/markdown"), vectors
    )

    result = service.collect("AI")

    assert result == {"found": 2, "stored": 1, "chunks": 2, "errors": 1}
    assert client.calls == [("AI", 7)]
    assert vectors.indexed == ["valid"]
    assert sorted(path.name for path in service.markdown.directory.glob("*.md")) == [
        "valid.md"
    ]
    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "rejected-id" in errors
    assert "missing or blank text" in errors
    assert "body" not in errors


def test_collect_persists_sanitized_failed_run_before_reraising_search_error(
    tmp_path: Path,
) -> None:
    failure = OpenCLIError(
        "request failed auth_token=TOKEN RECOGNIZABLE RESPONSE BODY"
    )

    class FailingOpenCLI:
        def search(self, keyword: str, limit: int) -> list[Post]:
            raise failure

    vectors = Vectors()
    markdown = MarkdownStore(tmp_path / "data/markdown")
    service = XragService(
        config(tmp_path),
        FailingOpenCLI(),
        markdown,
        vectors,
        clock=lambda: "2026-08-09T12:00:00Z",
    )

    with pytest.raises(OpenCLIError) as raised:
        service.collect("later-keyword")

    assert raised.value is failure
    assert not markdown.directory.exists()
    assert vectors.indexed == []
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run == {
        "operation": "collect",
        "time": "2026-08-09T12:00:00Z",
        "keyword": "later-keyword",
        "outcome": "failed",
        "counts": {"found": 0, "stored": 0, "chunks": 0, "errors": 1},
    }
    error_log = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "later-keyword" in error_log
    assert "OpenCLIError" in error_log
    assert "search failed" in error_log
    assert "TOKEN" not in error_log
    assert "RECOGNIZABLE" not in error_log
    assert "BODY" not in error_log


def test_collect_logs_only_row_index_for_a_rejected_secret_bearing_id(
    tmp_path: Path,
) -> None:
    payload = """
- id: "auth_token=TOPSECRET RECOGNIZABLE BODY"
  text: safe rejected text
- id: "123"
  text: safe valid text
"""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    vectors = Vectors()
    service = XragService(
        config(tmp_path),
        OpenCLIClient(run=run),
        MarkdownStore(tmp_path / "data/markdown"),
        vectors,
    )

    assert service.collect("AI") == {
        "found": 2,
        "stored": 1,
        "chunks": 2,
        "errors": 1,
    }
    assert vectors.indexed == ["123"]
    assert sorted(path.name for path in service.markdown.directory.glob("*.md")) == [
        "123.md"
    ]
    error_log = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "row[0]" in error_log
    assert "missing or invalid id" in error_log
    assert "TOPSECRET" not in error_log
    assert "RECOGNIZABLE" not in error_log
    assert "BODY" not in error_log
    assert "auth_token" not in error_log


@pytest.mark.parametrize("method_name", ["search_batch", "search"])
@pytest.mark.parametrize(
    "failure",
    [KeyboardInterrupt(), SystemExit(17)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_collect_does_not_persist_failure_for_base_exceptions_from_search(
    tmp_path: Path, method_name: str, failure: BaseException
) -> None:
    class InterruptingOpenCLI:
        def search_batch(self, keyword: str, limit: int) -> SearchBatch:
            if method_name == "search_batch":
                raise failure
            return SearchBatch((), ())

        def search(self, keyword: str, limit: int) -> list[Post]:
            raise failure

    client: object
    if method_name == "search_batch":
        client = InterruptingOpenCLI()
    else:
        class LegacyInterruptingOpenCLI:
            def search(self, keyword: str, limit: int) -> list[Post]:
                raise failure

        client = LegacyInterruptingOpenCLI()

    vectors = Vectors()
    markdown = MarkdownStore(tmp_path / "data/markdown")
    service = XragService(config(tmp_path), client, markdown, vectors)

    with pytest.raises(type(failure)) as raised:
        service.collect("AI")

    assert raised.value is failure
    assert not markdown.directory.exists()
    assert vectors.indexed == []
    assert not (tmp_path / "logs/last-run.json").exists()
    assert not (tmp_path / "logs/errors.jsonl").exists()


def test_collect_all_preserves_keyword_order_and_sleeps_only_between(tmp_path: Path) -> None:
    client = OpenCLI([])
    sleeps: list[int] = []
    service = XragService(config(tmp_path, ("one", "two", "three")), client, MarkdownStore(tmp_path / "md"), Vectors(), sleep=sleeps.append)

    results = service.collect_all()

    assert [keyword for keyword, _ in results] == ["one", "two", "three"]
    assert client.calls == [("one", 7), ("two", 7), ("three", 7)]
    assert sleeps == [3, 3]


def test_collect_all_records_later_failure_and_does_not_sleep_or_continue(
    tmp_path: Path,
) -> None:
    failure = OpenCLIError("second failed api_key=SECRET")

    class SequentialOpenCLI:
        calls: list[tuple[str, int]]

        def __init__(self) -> None:
            self.calls = []

        def search(self, keyword: str, limit: int) -> list[Post]:
            self.calls.append((keyword, limit))
            if keyword == "two":
                raise failure
            return []

    client = SequentialOpenCLI()
    sleeps: list[int] = []
    service = XragService(
        config(tmp_path, ("one", "two", "three")),
        client,
        MarkdownStore(tmp_path / "md"),
        Vectors(),
        sleep=sleeps.append,
        clock=lambda: "2026-08-09T12:00:00Z",
    )

    with pytest.raises(OpenCLIError) as raised:
        service.collect_all()

    assert raised.value is failure
    assert client.calls == [("one", 7), ("two", 7)]
    assert sleeps == [3]
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run["keyword"] == "two"
    assert last_run["outcome"] == "failed"
    assert last_run["counts"] == {
        "found": 0,
        "stored": 0,
        "chunks": 0,
        "errors": 1,
    }


def test_collect_preserves_an_explicit_zero_limit(tmp_path: Path) -> None:
    client = OpenCLI([])
    service = make_service(tmp_path, client, Vectors())

    service.collect("AI", limit=0)

    assert client.calls == [("AI", 0)]


@pytest.mark.parametrize("failure", [OSError("disk unavailable"), RuntimeError("append failed")])
def test_error_log_failure_is_best_effort_and_does_not_stop_later_posts(
    tmp_path: Path, failure: Exception
) -> None:
    class FailingLogService(XragService):
        append_attempts = 0

        def _append_error(self, path: Path, line: str) -> None:
            self.append_attempts += 1
            raise failure

    vectors = Vectors({"bad"})
    service = FailingLogService(
        config(tmp_path), OpenCLI([post("bad"), post("good")]),
        MarkdownStore(tmp_path / "data/markdown"), vectors,
    )

    assert service.collect("AI") == {"found": 2, "stored": 2, "chunks": 2, "errors": 1}
    assert service.append_attempts == 1
    assert vectors.indexed == ["bad", "good"]


def test_error_log_failure_does_not_mask_original_search_exception(
    tmp_path: Path,
) -> None:
    failure = OpenCLIError("auth_token=SECRET")

    class FailingOpenCLI:
        def search(self, keyword: str, limit: int) -> list[Post]:
            raise failure

    class FailingLogService(XragService):
        def _append_error(self, path: Path, line: str) -> None:
            raise OSError("log unavailable")

    service = FailingLogService(
        config(tmp_path),
        FailingOpenCLI(),
        MarkdownStore(tmp_path / "data/markdown"),
        Vectors(),
    )

    with pytest.raises(OpenCLIError) as raised:
        service.collect("AI")

    assert raised.value is failure
    last_run = json.loads((tmp_path / "logs/last-run.json").read_text(encoding="utf-8"))
    assert last_run["outcome"] == "failed"


def test_error_log_append_does_not_read_or_erase_existing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "logs/errors.jsonl"
    log_path.parent.mkdir(parents=True)
    log_path.write_text('{"old":"history"}\n', encoding="utf-8", newline="\n")
    original_read_text = Path.read_text

    def reject_error_log_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == log_path:
            raise RuntimeError("errors.jsonl must not be read before append")
        return original_read_text(path, *args, **kwargs)

    vectors = Vectors({"bad"})
    service = make_service(tmp_path, OpenCLI([post("bad")]), vectors)
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "read_text", reject_error_log_read)
        service.collect("AI")

    contents = log_path.read_text(encoding="utf-8")
    assert contents.startswith('{"old":"history"}\n')
    assert contents.count("\n") == 2


def test_import_directory_sorts_files_rejects_partial_bad_file_and_skips_canonical(tmp_path: Path) -> None:
    source = tmp_path
    (source / "b.json").write_text('[{"id":"b","text":"B"}]', encoding="utf-8")
    (source / "a.yaml").write_text("- id: would-be-partial\n  text: okay\n- id: broken\n", encoding="utf-8")
    canonical = source / "data/markdown"
    canonical.mkdir(parents=True)
    (canonical / "generated.md").write_text("---\nid: generated\n---\nshould skip\n", encoding="utf-8")
    vectors = Vectors()
    service = make_service(tmp_path, OpenCLI(), vectors)

    result = service.import_path(source)

    assert result == {"files": 2, "imported": 1, "chunks": 2, "errors": 1}
    assert vectors.indexed == ["b"]
    assert not (canonical / "would-be-partial.md").exists()
    error_log = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "a.yaml" in error_log and "text" in error_log


def test_import_directory_uses_path_sort_order(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('{"id":"lower","text":"a"}', encoding="utf-8")
    (tmp_path / "B.json").write_text('{"id":"upper","text":"B"}', encoding="utf-8")
    vectors = Vectors()
    service = make_service(tmp_path, OpenCLI(), vectors)

    service.import_path(tmp_path)

    assert vectors.indexed == ["upper", "lower"]


def test_import_uses_injected_markdown_directory_for_exclusion_and_rejects_it_as_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "valid.json").write_text('{"id":"valid","text":"okay"}', encoding="utf-8")
    canonical = source / "custom-canonical"
    markdown = MarkdownStore(canonical)
    generated = markdown.upsert(post("generated"))
    vectors = Vectors()
    service = XragService(config(tmp_path), OpenCLI(), markdown, vectors)

    assert service.import_path(source) == {"files": 1, "imported": 1, "chunks": 2, "errors": 0}
    assert vectors.indexed == ["valid"]
    with pytest.raises(ValueError, match="canonical Markdown"):
        service.import_path(generated)


def test_import_single_file_and_clear_validation_errors(tmp_path: Path) -> None:
    source = tmp_path / "one.yml"
    source.write_text("id: one\ntext: hello\n", encoding="utf-8")
    service = make_service(tmp_path, OpenCLI(), Vectors())

    assert service.import_path(source) == {"files": 1, "imported": 1, "chunks": 2, "errors": 0}
    with pytest.raises(ValueError, match="does not exist"):
        service.import_path(tmp_path / "missing")
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        service.import_path(unsupported)


def test_import_last_run_does_not_copy_a_sensitive_source_path(tmp_path: Path) -> None:
    source = tmp_path / "auth_token=path-secret.json"
    source.write_text('{"id":"one","text":"safe"}', encoding="utf-8")
    service = make_service(tmp_path, OpenCLI(), Vectors())

    service.import_path(source)

    last_run = (tmp_path / "logs/last-run.json").read_text(encoding="utf-8")
    assert "path-secret" not in last_run


def test_import_error_log_removes_secrets_and_body_from_multiline_parser_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "malformed.yaml"
    source.write_text(
        '{"auth_token":"TOKEN","ct0":"COOKIE","TWITTER_AUTH_TOKEN":"ENV_TOKEN",'
        '"X_CT0":"ENV_COOKIE","body":"RECOGNIZABLE BODY", broken\n',
        encoding="utf-8",
    )
    service = make_service(tmp_path, OpenCLI(), Vectors())

    assert service.import_path(source) == {
        "files": 1,
        "imported": 0,
        "chunks": 0,
        "errors": 1,
    }

    errors = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert "ValueError" in errors and "Cannot load YAML" in errors
    assert "TOKEN" not in errors
    assert "COOKIE" not in errors
    assert "ENV_TOKEN" not in errors
    assert "ENV_COOKIE" not in errors
    assert "RECOGNIZABLE BODY" not in errors


@pytest.mark.parametrize(
    "message, secret",
    [
        ("api_key=APISECRET", "APISECRET"),
        ('"api-key": "DASHSECRET"', "DASHSECRET"),
        ("password='PASSWORDSECRET'", "PASSWORDSECRET"),
        ("passwd: PASSWDSECRET", "PASSWDSECRET"),
        ('"client_secret":"CLIENTSECRET"', "CLIENTSECRET"),
        ("access_token=ACCESSSECRET", "ACCESSSECRET"),
        ("refresh_token: 'REFRESHSECRET'", "REFRESHSECRET"),
        ('"authorization": "Basic AUTHSECRET"', "AUTHSECRET"),
        ("request rejected: Bearer BEARERSECRET", "BEARERSECRET"),
        ("authorization: Bearer COMBINEDSECRET", "COMBINEDSECRET"),
    ],
)
def test_non_post_error_summary_scrubs_common_secret_forms(
    tmp_path: Path, message: str, secret: str
) -> None:
    service = make_service(tmp_path, OpenCLI(), Vectors())

    service._log_error("import", "fixture", ValueError(message))

    assert secret not in (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "message, payloads",
    [
        ("Authorization: Basic dXNlcjpwYXNz", ("dXNlcjpwYXNz",)),
        ("authorization=Token TOPSECRET", ("TOPSECRET",)),
        (
            "Authorization: Digest username=x, response=y",
            ("username=x", "response=y"),
        ),
    ],
)
def test_authorization_summary_redacts_complete_unquoted_credentials(
    tmp_path: Path, message: str, payloads: tuple[str, ...]
) -> None:
    service = make_service(tmp_path, OpenCLI(), Vectors())

    service._log_error("import", "fixture", ValueError(message))

    error_log = (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8")
    assert all(payload not in error_log for payload in payloads)


@pytest.mark.parametrize(
    "credential, payloads",
    [
        ("TWITTER_AUTHORIZATION=Basic LEAKME", ("LEAKME",)),
        ("X_AUTHORIZATION: Token TOPSECRET", ("TOPSECRET",)),
        (
            "TWITTER_AUTHORIZATION: Digest username=alice, response=deadbeef",
            ("username=alice", "response=deadbeef"),
        ),
        (
            "X_AUTHORIZATION=OAuth oauth_token=OAUTHLEAK, oauth_signature=SIGLEAK",
            ("OAUTHLEAK", "SIGLEAK"),
        ),
        (
            "TWITTER_AUTHORIZATION: Custom alpha=ONELEAK, beta=TWOLEAK",
            ("ONELEAK", "TWOLEAK"),
        ),
    ],
)
def test_prefixed_authorization_is_fully_redacted_in_error_source_and_reason(
    tmp_path: Path, credential: str, payloads: tuple[str, ...]
) -> None:
    service = make_service(tmp_path, OpenCLI(), Vectors())

    service._log_error("import", credential, ValueError(credential))

    [record] = [
        json.loads(line)
        for line in (tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert record["operation"] == "import"
    assert record["error"] == "ValueError"
    assert all(payload not in record["source"] for payload in payloads)
    assert all(payload not in record["message"] for payload in payloads)


def test_rebuild_without_factory_refuses_to_touch_old_index(tmp_path: Path) -> None:
    stable = tmp_path / "data/chroma"
    stable.mkdir(parents=True)
    sentinel = stable / "old-sentinel"
    sentinel.write_bytes(b"preserve")
    vectors = Vectors()
    service = XragService(
        config(tmp_path), OpenCLI(), MarkdownStore(tmp_path / "data/markdown"), vectors
    )

    with pytest.raises(RuntimeError, match="factory"):
        service.rebuild()

    assert sentinel.read_bytes() == b"preserve"
    assert vectors.cleared == 0


def test_search_delegates_and_status_handles_empty_valid_and_malformed_last_run(tmp_path: Path) -> None:
    vectors = Vectors()
    service = make_service(tmp_path, OpenCLI(), vectors)
    assert service.search("needle", 4) == ["needle"]
    assert vectors.search_args == ("needle", 4)
    assert service.status() == {
        "documents": 0,
        "document_errors": 0,
        "chunks": 0,
        "keywords": 2,
        "last_run": None,
    }

    service.collect("AI")
    assert service.status()["last_run"]["operation"] == "collect"
    (tmp_path / "logs/last-run.json").write_text("{broken", encoding="utf-8")
    malformed = service.status()
    assert malformed["last_run"] is None
    assert "error" in malformed["last_run_status"].lower()


def test_status_counts_malformed_canonical_files_and_reports_document_errors(tmp_path: Path) -> None:
    markdown = MarkdownStore(tmp_path / "data/markdown")
    markdown.upsert(post("valid"))
    (markdown.directory / "broken.md").write_text("not front matter\n", encoding="utf-8")
    service = XragService(config(tmp_path), OpenCLI(), markdown, Vectors())

    status = service.status()

    assert status["documents"] == 2
    assert status["document_errors"] == 1
    assert status["chunks"] == 0
    assert status["keywords"] == 2


def test_collect_writes_last_run_before_releasing_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_active = False

    @contextmanager
    def tracking_lock(root: Path, timeout: float = 1):
        nonlocal lock_active
        lock_active = True
        try:
            yield
        finally:
            lock_active = False

    class ObservedService(XragService):
        last_run_lock_states: list[bool] = []

        def _write_last_run(self, operation: str, counts: dict[str, int], **context: str) -> None:
            self.last_run_lock_states.append(lock_active)
            super()._write_last_run(operation, counts, **context)

    monkeypatch.setattr(service_module, "writer_lock", tracking_lock)
    service = ObservedService(
        config(tmp_path), OpenCLI(), MarkdownStore(tmp_path / "data/markdown"), Vectors()
    )

    service.collect("AI")

    assert service.last_run_lock_states == [True]
