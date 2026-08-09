from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
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


def test_collect_all_rejects_limit_without_initialization(monkeypatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(cli, "build_service", lambda root: calls.append(root))

    result = runner.invoke(cli.app, ["collect", "--all", "--limit", "5"])

    assert result.exit_code == 2
    assert "--limit" in result.stderr
    assert "--all" in result.stderr
    assert calls == []


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


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("TWITTER_AUTH_TOKEN=twitter-auth-value", "twitter-auth-value"),
        ("TWITTER_CT0: 'twitter-ct0-value'", "twitter-ct0-value"),
        ('"X_AUTH_TOKEN": "x-auth-value"', "x-auth-value"),
        ("X_CT0=x-ct0-value", "x-ct0-value"),
        ("TWITTER_API_KEY=twitter-api-value", "twitter-api-value"),
        ('"X_ACCESS_TOKEN": "x-access-value"', "x-access-value"),
        ("TWITTER_PASSWORD: 'twitter-password-value'", "twitter-password-value"),
        ("X_REFRESH_TOKEN=x-refresh-value", "x-refresh-value"),
        ('"TWITTER_CLIENT_SECRET"="twitter-client-value"', "twitter-client-value"),
        ("X_AUTHORIZATION: Basic prefixed-basic-value", "prefixed-basic-value"),
        ("Authorization: Bearer bearer-value", "bearer-value"),
        ("Authorization=Token token-value", "token-value"),
        (
            'Authorization: Digest username="user", response="digest-value"',
            "digest-value",
        ),
        ("auth_token=plain-auth-value", "plain-auth-value"),
        ('"ct0": "plain-ct0-value"', "plain-ct0-value"),
        ("api_key: api-value", "api-value"),
    ],
)
def test_operational_error_redacts_credential_variants(
    monkeypatch, message: str, secret: str
) -> None:
    monkeypatch.setattr(
        cli,
        "build_service",
        lambda root: (_ for _ in ()).throw(OpenCLIError(f"browser failed: {message}")),
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "Error: browser failed" in result.stderr
    assert "[REDACTED]" in result.stderr
    assert secret not in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("authorization", "payload_fragments"),
    [
        (
            'OAuth realm="Example", oauth_consumer_key="consumer", '
            'oauth_token="token", oauth_signature="signature"',
            ("Example", "consumer", "token", "signature"),
        ),
        (
            "AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE, SignedHeaders=host, "
            "Signature=aws-signature",
            ("AKIAEXAMPLE", "SignedHeaders", "aws-signature"),
        ),
    ],
)
def test_authorization_redaction_removes_complete_field_line(
    monkeypatch, authorization: str, payload_fragments: tuple[str, ...]
) -> None:
    message = f"browser failed\nAuthorization: {authorization}\nretry later"
    monkeypatch.setattr(
        cli,
        "build_service",
        lambda root: (_ for _ in ()).throw(OpenCLIError(message)),
    )

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "authorization=[REDACTED]" in result.stderr
    assert "retry later" in result.stderr
    assert all(fragment not in result.stderr for fragment in payload_fragments)
    assert "Traceback" not in result.output


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, OSError])
@pytest.mark.parametrize("failure_point", ["build", "service"])
def test_known_operational_exceptions_exit_cleanly(
    monkeypatch, error_type: type[Exception], failure_point: str
) -> None:
    error = error_type("failed auth_token=never-print-this")
    if failure_point == "build":
        monkeypatch.setattr(
            cli, "build_service", lambda root: (_ for _ in ()).throw(error)
        )
    else:
        service = FakeService()
        service.status = lambda: (_ for _ in ()).throw(error)
        install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 2
    assert "Error: failed" in result.stderr
    assert "[REDACTED]" in result.stderr
    assert "never-print-this" not in result.output
    assert "Traceback" not in result.output


def test_keyboard_interrupt_is_left_to_click(monkeypatch) -> None:
    service = FakeService()
    service.status = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["status"], catch_exceptions=False)

    assert result.exit_code == 130
    assert isinstance(result.exception, SystemExit)
    assert "Error:" not in result.output


def test_system_exit_is_not_caught_as_an_operational_error(monkeypatch) -> None:
    service = FakeService()
    service.status = lambda: (_ for _ in ()).throw(SystemExit(17))
    install_fake(monkeypatch, service)

    result = runner.invoke(cli.app, ["status"], catch_exceptions=False)

    assert result.exit_code == 17
    assert "Error:" not in result.output


def test_no_arguments_show_help_without_building_service(monkeypatch) -> None:
    roots: list[Path] = []
    monkeypatch.setattr(cli, "build_service", lambda root: roots.append(root))

    result = runner.invoke(cli.app, [])

    assert "Usage:" in result.stdout
    assert "collect" in result.stdout
    assert roots == []
    assert result.exit_code == 2


def test_utf8_stream_configuration_handles_cp936_redirects(monkeypatch) -> None:
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp936", errors="strict")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp936", errors="strict")
    monkeypatch.setattr(cli, "sys", SimpleNamespace(stdout=stdout, stderr=stderr))

    cli._configure_utf8_streams()
    stdout.write("中文😀")
    stderr.write("错误🚫")
    stdout.flush()
    stderr.flush()

    assert stdout.encoding.lower().replace("-", "") == "utf8"
    assert stderr.encoding.lower().replace("-", "") == "utf8"
    assert stdout_bytes.getvalue().decode("utf-8") == "中文😀"
    assert stderr_bytes.getvalue().decode("utf-8") == "错误🚫"


def test_module_entrypoint_help_does_not_initialize_model(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "src")

    result = subprocess.run(
        [sys.executable, "-m", "xrag.cli", "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert "collect" in result.stdout
    assert "Failed to initialize" not in result.stderr


def test_installed_console_entrypoint_help_does_not_initialize_model(tmp_path: Path) -> None:
    executable_name = "xrag.exe" if os.name == "nt" else "xrag"
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), f"editable console script is not installed: {executable}"

    result = subprocess.run(
        [str(executable), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 0
    assert "collect" in result.stdout
    assert "Failed to initialize" not in result.stderr


def test_installed_console_reports_malformed_yaml_without_traceback(tmp_path: Path) -> None:
    executable_name = "xrag.exe" if os.name == "nt" else "xrag"
    executable = Path(sys.executable).with_name(executable_name)
    assert executable.is_file(), f"editable console script is not installed: {executable}"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "keywords.yaml").write_text(
        "keywords: [unterminated", encoding="utf-8"
    )

    result = subprocess.run(
        [str(executable), "--root", str(tmp_path), "status"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert "Error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_console_entrypoint_targets_typer_app() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert 'xrag = "xrag.cli:app"' in pyproject
