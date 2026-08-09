from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run-daily.sh"
INSTALLER = PROJECT_ROOT / "scripts" / "install-schedule.ps1"
INSTALLER_RELATIVE = ".\\scripts\\install-schedule.ps1"


def powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    return executable


def run_powershell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            powershell(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def test_daily_runner_derives_root_and_appends_exact_collect_command() -> None:
    script = RUNNER.read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert 'dirname -- "${BASH_SOURCE[0]}"' in script
    assert 'PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"' in script
    assert 'mkdir -p -- "$PROJECT_ROOT/logs"' in script
    assert (
        'exec "$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" collect --all '
        '>> "$PROJECT_ROOT/logs/scheduler.log" 2>&1'
    ) in script


def test_installer_has_idempotent_daily_current_user_defaults() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert '[string]$Distribution = "Ubuntu"' in script
    assert '[string]$ScheduleTime = "10:00"' in script
    assert '$TaskName = "X-RAG Daily Collection"' in script
    assert "New-ScheduledTaskTrigger -Daily -At $ScheduleTime" in script
    assert "New-ScheduledTaskSettingsSet" in script
    assert "-StartWhenAvailable" in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "Register-ScheduledTask" in script
    assert "-Force" in script
    assert "New-ScheduledTaskPrincipal" in script
    assert "-LogonType Interactive" in script
    assert "-RunLevel Limited" in script


def test_installer_parses_as_powershell() -> None:
    parser_command = (
        "$tokens = $null; $errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile({ps_literal(INSTALLER_RELATIVE)}, "
        "[ref]$tokens, [ref]$errors); "
        "if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
    )

    result = run_powershell(parser_command)

    assert result.returncode == 0, result.stderr


def test_dry_run_uses_stubbed_wsl_translation_and_sanitized_quoted_action() -> None:
    fake_wsl_runner = "/mnt/c/Users/test user/project with spaces/scripts/run-daily.sh"
    command = (
        f"function global:wsl.exe {{ {ps_literal(fake_wsl_runner)} }}; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun"
    )

    result = run_powershell(command)

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert "X-RAG Daily Collection" in result.stdout
    assert "10:00" in result.stdout
    assert "wsl.exe" in result.stdout
    assert '-d "Ubuntu" -- bash "' + fake_wsl_runner + '"' in result.stdout
    assert "Register-ScheduledTask" not in result.stdout


@pytest.mark.parametrize("invalid_time", ["9:00", "24:00", "10:60", "noon"])
def test_invalid_schedule_time_fails_before_wsl_or_registration(invalid_time: str) -> None:
    command = (
        "function global:wsl.exe { throw 'WSL SHOULD NOT RUN' }; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun -ScheduleTime {ps_literal(invalid_time)}"
    )

    result = run_powershell(command)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "HH:mm" in output
    assert "WSL SHOULD NOT RUN" not in output


def test_scheduler_scripts_do_not_contain_credentials() -> None:
    combined = (RUNNER.read_text(encoding="utf-8") + INSTALLER.read_text(encoding="utf-8")).lower()

    for forbidden in ("auth_token", "ct0", "credentials"):
        assert forbidden not in combined


def test_daily_runner_is_kept_with_lf_line_endings_by_git() -> None:
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", "scripts/run-daily.sh"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip().endswith("eol: lf")
