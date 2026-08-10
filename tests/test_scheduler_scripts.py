from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
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
    assert 'exec >> "$PROJECT_ROOT/logs/scheduler.log" 2>&1' in script
    assert 'export PATH="$HOME/.local/bin:$PATH"' in script
    assert "export HF_HUB_OFFLINE=1" in script
    assert "export TRANSFORMERS_OFFLINE=1" in script
    assert "command -v opencli" in script
    assert "scheduled collection start" in script
    assert 'exec "$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" collect --all' in script


def test_readme_documents_local_media_and_resilient_collection() -> None:
    readme = PROJECT_ROOT.joinpath("README.md").read_text(encoding="utf-8")

    assert "data/media/<推文ID>/" in readme
    assert "视频只下载封面" in readme
    assert "正文只有短链接" in readme
    assert "每组每天采集 10 条" in readme
    assert "图片下载失败" in readme


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


def test_installer_decodes_direct_wsl_output_as_utf8_and_keeps_stderr_separate() -> None:
    script = INSTALLER.read_text(encoding="utf-8")

    assert "& wsl.exe -d $Distribution -e wslpath -a $RunnerWindowsPath" in script
    assert "2>&1" not in script
    assert "$previousConsoleOutputEncoding = [Console]::OutputEncoding" in script
    assert "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)" in script
    assert "[Console]::OutputEncoding = $previousConsoleOutputEncoding" in script


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
        "$global:CapturedWslArgs = @(); "
        f"function global:wsl.exe {{ $global:CapturedWslArgs = @($args); "
        f"$global:LASTEXITCODE = 0; {ps_literal(fake_wsl_runner)} }}; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun; "
        "$expectedRunner = (Resolve-Path -LiteralPath '.\\scripts\\run-daily.sh').Path; "
        "if ($global:CapturedWslArgs.Count -ne 6) { throw 'wrong argv count' }; "
        "if ($global:CapturedWslArgs[0] -cne '-d' -or "
        "$global:CapturedWslArgs[1] -cne 'Ubuntu' -or "
        "$global:CapturedWslArgs[2] -cne '-e' -or "
        "$global:CapturedWslArgs[3] -cne 'wslpath' -or "
        "$global:CapturedWslArgs[4] -cne '-a' -or "
        "$global:CapturedWslArgs[5] -cne $expectedRunner) { throw 'wrong argv values' }; "
        "'ARGV_OK'"
    )

    result = run_powershell(command)

    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert "X-RAG Daily Collection" in result.stdout
    assert "10:00" in result.stdout
    assert "wsl.exe" in result.stdout
    assert '-d Ubuntu -e bash "' + fake_wsl_runner + '"' in result.stdout
    assert "ARGV_OK" in result.stdout
    assert "Register-ScheduledTask" not in result.stdout


def test_wsl_stderr_with_zero_exit_does_not_fail_translation() -> None:
    command = (
        "function global:wsl.exe { Write-Error 'benign wsl warning'; "
        "$global:LASTEXITCODE = 0; '/mnt/c/project/scripts/run-daily.sh' }; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun; 'STDERR_OK'"
    )

    result = run_powershell(command)

    assert result.returncode == 0, result.stderr
    assert "STDERR_OK" in result.stdout
    assert "Dry run" in result.stdout


def test_wsl_stderr_with_nonzero_exit_fails_with_context() -> None:
    command = (
        "function global:wsl.exe { Write-Error 'translation detail'; "
        "$global:LASTEXITCODE = 17 }; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun"
    )

    result = run_powershell(command)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Ubuntu" in output
    assert "exit code 17" in output
    assert "NativeCommandError" not in output


def test_scheduled_action_uses_direct_exec_and_preserves_literal_path() -> None:
    command = (
        "$global:FakeWslRunner = \"/mnt/c/path with spaces/`$(throw 'EXPANDED')/\" + "
        "[char]0x4E2D + [char]0x6587 + '/run-daily.sh'; "
        "function global:wsl.exe { $global:LASTEXITCODE = 0; $global:FakeWslRunner }; "
        "function global:New-ScheduledTaskAction { param($Execute, $Argument); "
        "if ($Execute -cne 'wsl.exe') { throw 'wrong executable' }; "
        "if ($Argument -cne ('-d Ubuntu -e bash \"' + $global:FakeWslRunner + '\"')) "
        "{ throw ('wrong action: ' + $Argument) }; "
        "$global:ActionOk = $true; [pscustomobject]@{} }; "
        "function global:New-ScheduledTaskTrigger { param([switch]$Daily, $At); [pscustomobject]@{} }; "
        "function global:New-ScheduledTaskSettingsSet { param([switch]$StartWhenAvailable, $MultipleInstances); [pscustomobject]@{} }; "
        "function global:New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel); [pscustomobject]@{} }; "
        "function global:Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Principal, [switch]$Force); "
        "if (-not $Force) { throw 'missing force' }; $global:Registered = $true }; "
        f"& {ps_literal(INSTALLER_RELATIVE)}; "
        "if (-not $global:ActionOk) { throw 'action stub was not called' }; "
        "if (-not $global:Registered) { throw 'register stub was not called' }; "
        "'ACTION_OK'; 'REGISTERED'"
    )

    result = run_powershell(command)

    assert result.returncode == 0, result.stderr
    assert "ACTION_OK" in result.stdout
    assert "REGISTERED" in result.stdout
    assert "EXPANDED" not in result.stderr


def test_dry_run_never_calls_scheduler_cmdlets() -> None:
    command = (
        "function global:wsl.exe { $global:LASTEXITCODE = 0; '/mnt/c/project/scripts/run-daily.sh' }; "
        "function global:New-ScheduledTaskAction { throw 'SCHEDULER CALLED' }; "
        "function global:New-ScheduledTaskTrigger { throw 'SCHEDULER CALLED' }; "
        "function global:New-ScheduledTaskSettingsSet { throw 'SCHEDULER CALLED' }; "
        "function global:New-ScheduledTaskPrincipal { throw 'SCHEDULER CALLED' }; "
        "function global:Register-ScheduledTask { throw 'SCHEDULER CALLED' }; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun; 'DRY_RUN_SAFE'"
    )

    result = run_powershell(command)

    assert result.returncode == 0, result.stderr
    assert "DRY_RUN_SAFE" in result.stdout
    assert "SCHEDULER CALLED" not in result.stdout + result.stderr


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


@pytest.mark.parametrize("invalid_distribution", ["Ubuntu Name", "Ubuntu;whoami", "$(whoami)", 'Ubuntu"bad'])
def test_invalid_distribution_fails_before_wsl(invalid_distribution: str) -> None:
    command = (
        "function global:wsl.exe { throw 'WSL SHOULD NOT RUN' }; "
        f"& {ps_literal(INSTALLER_RELATIVE)} -DryRun -Distribution {ps_literal(invalid_distribution)}"
    )

    result = run_powershell(command)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "Distribution" in output
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
        check=False,
    )
    if result.returncode != 0 and os.name != "nt":
        pytest.skip("Windows-linked worktree metadata is not readable by WSL git")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("eol: lf")


def translate_with_wsl(path: Path) -> str:
    if os.name != "nt":
        return str(path)

    translated = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-e", "wslpath", "-a", str(path)],
        capture_output=True,
        check=False,
    )
    if translated.returncode != 0:
        pytest.skip(f"Ubuntu WSL probe unavailable: {translated.stderr!r}")
    return translated.stdout.decode("utf-8").strip()


def prepare_wsl_runner(tmp_path: Path, *, with_opencli: bool) -> tuple[Path, str, str]:
    if os.name == "nt" and shutil.which("wsl.exe") is None:
        pytest.skip("WSL is not installed")

    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    fake_bin = project / ".venv" / "bin"
    fake_home = project / "fake home"
    local_bin = fake_home / ".local" / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir(parents=True)
    local_bin.mkdir(parents=True)
    (scripts / "run-daily.sh").write_bytes(RUNNER.read_bytes())
    fake_xrag = fake_bin / "xrag"
    fake_xrag.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'ARG=<%s>\\n' \"$@\"\n"
        "printf 'OPENCLI=<%s>\\n' \"$(command -v opencli)\"\n"
        "printf 'HF_HUB_OFFLINE=<%s>\\n' \"${HF_HUB_OFFLINE-}\"\n"
        "printf 'TRANSFORMERS_OFFLINE=<%s>\\n' \"${TRANSFORMERS_OFFLINE-}\"\n"
        "printf 'XRAG_RAN\\n'\n"
        "exit \"${FAKE_STATUS:-0}\"\n",
        encoding="utf-8",
        newline="\n",
    )
    fake_xrag.chmod(0o755)
    if with_opencli:
        fake_opencli = local_bin / "opencli"
        fake_opencli.write_text(
            "#!/usr/bin/env bash\nexit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        fake_opencli.chmod(0o755)

    return project, translate_with_wsl(scripts / "run-daily.sh"), translate_with_wsl(fake_home)


def run_daily_in_minimal_environment(wsl_runner: str, wsl_home: str, status: int = 0) -> subprocess.CompletedProcess[bytes]:
    if os.name != "nt":
        return subprocess.run(
            ["env", "-i", f"HOME={wsl_home}", "PATH=/usr/bin:/bin", f"FAKE_STATUS={status}", "bash", wsl_runner],
            capture_output=True,
            check=False,
        )

    return subprocess.run(
        [
            "wsl.exe",
            "-d",
            "Ubuntu",
            "-e",
            "env",
            "-i",
            f"HOME={wsl_home}",
            "PATH=/usr/bin:/bin",
            f"FAKE_STATUS={status}",
            "bash",
            wsl_runner,
        ],
        capture_output=True,
        check=False,
    )


def test_daily_runner_finds_opencli_and_sets_offline_environment(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home = prepare_wsl_runner(tmp_path, with_opencli=True)

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    wsl_project = str(PurePosixPath(wsl_runner).parents[1])
    assert "scheduled collection start" in log
    assert [line for line in log.splitlines() if line.startswith("ARG=")] == [
        "ARG=<--root>",
        f"ARG=<{wsl_project}>",
        "ARG=<collect>",
        "ARG=<--all>",
    ]
    assert f"OPENCLI=<{wsl_home}/.local/bin/opencli>" in log
    assert "HF_HUB_OFFLINE=<1>" in log
    assert "TRANSFORMERS_OFFLINE=<1>" in log


def test_daily_runner_exits_127_before_xrag_when_opencli_is_absent(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home = prepare_wsl_runner(tmp_path, with_opencli=False)

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home)

    assert result.returncode == 127, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "scheduled collection start" in log
    assert "opencli" in log.lower()
    assert "not found" in log.lower()
    assert "XRAG_RAN" not in log


def test_daily_runner_propagates_xrag_nonzero_status(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home = prepare_wsl_runner(tmp_path, with_opencli=True)

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home, status=23)

    assert result.returncode == 23, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "XRAG_RAN" in log
