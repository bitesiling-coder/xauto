from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from xrag import cli
from xrag.models import SearchHit
from xrag.opencli import OpenCLIError


runner = CliRunner()


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def collect(self, keyword: str, limit: int | None = None) -> dict[str, int]:
        self.calls.append(("collect", keyword, limit))
        return {"found": 3, "stored": 2, "chunks": 4, "errors": 1}

    def collect_all(self) -> list[tuple[str, dict[str, int]]]:
        self.calls.append(("collect_all",))
        return [
            ("人工智能", {"found": 2, "stored": 2, "chunks": 3, "errors": 0}),
            ("Python", {"found": 1, "stored": 1, "chunks": 1, "errors": 0}),
        ]

    def import_path(self, source: Path) -> dict[str, int]:
        self.calls.append(("import", source))
        return {"files": 1, "imported": 2, "chunks": 3, "errors": 0}

    def search(self, query: str, top: int) -> list[SearchHit]:
        self.calls.append(("search", query, top))
        return []

    def status(self) -> dict[str, object]:
        self.calls.append(("status",))
        return {"documents": 2, "label": "中文"}

    def rebuild(self) -> dict[str, int]:
        self.calls.append(("rebuild",))
        return {"documents": 2, "chunks": 5, "errors": 0}


def install_fake(monkeypatch, service: FakeService) -> list[Path]:
    roots: list[Path] = []

    def fake_build(root: Path) -> FakeService:
        roots.append(root)
        return service

    monkeypatch.setattr(cli, "build_service", fake_build)
    return roots


def test_collect_keyword_prints_summary_and_forwards_arguments(monkeypatch, tmp_path: Path) -> None:
    service = FakeService()
    roots = install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["--root", str(tmp_path), "collect", "AI", "--limit", "7"])

    assert result.exit_code == 0
    assert result.stdout == "AI: found=3 stored=2 chunks=4 errors=1\n"
    assert service.calls == [("collect", "AI", 7)]
    assert roots == [tmp_path.resolve()]


def test_collect_all_prints_one_summary_per_keyword(monkeypatch) -> None:
    service = FakeService()
    install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["collect", "--all"])

    assert result.exit_code == 0
    assert result.stdout == (
        "人工智能: found=2 stored=2 chunks=3 errors=0\n"
        "Python: found=1 stored=1 chunks=1 errors=0\n"
    )
    assert service.calls == [("collect_all",)]


def test_collect_rejects_missing_or_conflicting_mode_without_initialization(monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(cli, "build_service", lambda root: calls.append(root))

    missing = runner.invoke(cli.app, ["collect"])
    conflicting = runner.invoke(cli.app, ["collect", "AI", "--all"])

    assert missing.exit_code == 2
    assert conflicting.exit_code == 2
    assert "exactly one" in missing.output.lower()
    assert "exactly one" in conflicting.output.lower()
    assert calls == []


def test_collect_rejects_nonpositive_limit(monkeypatch) -> None:
    monkeypatch.setattr(cli, "build_service", lambda root: (_ for _ in ()).throw(AssertionError()))

    result = runner.invoke(cli.app, ["collect", "AI", "--limit", "0"])

    assert result.exit_code == 2
    assert "1" in result.output


def test_search_formats_hits(monkeypatch) -> None:
    service = FakeService()
    service.search = lambda query, top: [
        SearchHit(
            post_id="42", text="A useful chunk", author="alice",
            created_at="2026-01-02T03:04:05Z", url="https://x.com/alice/status/42",
            score=0.87594, markdown_path="C:/archive/42.md",
        )
    ]
    install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["search", "useful", "--top", "3"])

    assert result.exit_code == 0
    assert result.stdout == (
        "1. [0.8759] @alice · 2026-01-02T03:04:05Z\n"
        "A useful chunk\n"
        "URL: https://x.com/alice/status/42\n"
        "Markdown: C:/archive/42.md\n"
    )


def test_search_reports_no_results(monkeypatch) -> None:
    install_fake(monkeypatch, FakeService())

    result = runner.invoke(cli.app, ["search", "nothing"])

    assert result.exit_code == 0
    assert result.stdout == "No results found.\n"


def test_import_status_and_rebuild_emit_unicode_json(monkeypatch, tmp_path: Path) -> None:
    service = FakeService()
    install_fake(monkeypatch, service)
    source = tmp_path / "输入.json"
    source.write_text("[]", encoding="utf-8")

    imported = runner.invoke(cli.app, ["import", str(source)])
    status = runner.invoke(cli.app, ["status"])
    rebuilt = runner.invoke(cli.app, ["rebuild"])

    assert imported.exit_code == status.exit_code == rebuilt.exit_code == 0
    assert json.loads(imported.stdout) == {"files": 1, "imported": 2, "chunks": 3, "errors": 0}
    assert "中文" in status.stdout and "\\u4e2d" not in status.stdout
    assert json.loads(rebuilt.stdout) == {"documents": 2, "chunks": 5, "errors": 0}
    assert service.calls[0] == ("import", source)


def test_operational_error_is_redacted_and_has_no_traceback(monkeypatch) -> None:
    def fail(root: Path) -> object:
        raise OpenCLIError(
            'browser failed "auth_token": "secret" ct0: \'cookie-value\' api_key=key123 '
            "Authorization: Basic YWJjZA=="
        )

    monkeypatch.setattr(cli, "build_service", fail)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "Error: browser failed" in result.stderr
    assert "[REDACTED]" in result.stderr
    assert "secret" not in result.output
    assert "cookie-value" not in result.output
    assert "key123" not in result.output
    assert "YWJjZA" not in result.output
    assert "Traceback" not in result.output


def test_help_never_builds_service(monkeypatch) -> None:
    monkeypatch.setattr(cli, "build_service", lambda root: (_ for _ in ()).throw(AssertionError()))

    root_help = runner.invoke(cli.app, ["--help"])
    command_help = runner.invoke(cli.app, ["search", "--help"])

    assert root_help.exit_code == 0
    assert command_help.exit_code == 0
    assert "collect" in root_help.stdout
    assert "--top" in command_help.stdout
