from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER = PROJECT_ROOT / "scripts" / "run-daily.sh"
PUBLISH_WRAPPER = PROJECT_ROOT / "scripts" / "publish-dashboard.py"
INSTALLER = PROJECT_ROOT / "scripts" / "install-schedule.ps1"
INSTALLER_RELATIVE = ".\\scripts\\install-schedule.ps1"


def powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed")
    return executable


def run_powershell(command: str, *, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess[str]:
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
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def require_windows_wsl_git() -> None:
    if os.name != "nt" or shutil.which("wsl.exe") is None:
        pytest.skip("Windows Git plus WSL are required for pointer integration tests")
    probe = subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-e", "git", "--version"],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip("Ubuntu WSL Git is unavailable")


def install_scheduler_scripts(project: Path) -> None:
    scripts = project / "scripts"
    scripts.mkdir(exist_ok=True)
    shutil.copy2(INSTALLER, scripts / INSTALLER.name)
    shutil.copy2(RUNNER, scripts / RUNNER.name)


def create_normal_scheduler_repo(tmp_path: Path) -> Path:
    project = tmp_path / "normal project"
    project.mkdir(parents=True)
    assert run_git("init", "-b", "main", cwd=project).returncode == 0
    install_scheduler_scripts(project)
    return project


def create_linked_scheduler_repo(tmp_path: Path, *, with_pages: bool = True) -> tuple[Path, Path, Path]:
    root = tmp_path / "跨平台 测试"
    main = root / "main repo"
    feature = root / "feature 工作树"
    pages = feature / ".worktrees" / "x-rag-pages"
    main.mkdir(parents=True)
    assert run_git("init", "-b", "main", cwd=main).returncode == 0
    assert run_git("config", "user.name", "Scheduler Test", cwd=main).returncode == 0
    assert run_git("config", "user.email", "scheduler@example.invalid", cwd=main).returncode == 0
    assert run_git("commit", "--allow-empty", "-m", "initial", cwd=main).returncode == 0
    assert run_git("worktree", "add", "-b", "feature", str(feature), cwd=main).returncode == 0
    if with_pages:
        pages.parent.mkdir(parents=True)
        assert run_git("worktree", "add", "-b", "gh-pages", str(pages), cwd=main).returncode == 0

    install_scheduler_scripts(feature)
    return main, feature, pages


def run_stubbed_installer(
    project: Path,
    *,
    dry_run: bool = False,
    scheduler_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    installer = project / "scripts" / INSTALLER.name
    runner = project / "scripts" / RUNNER.name
    wsl_runner = translate_with_wsl(runner)
    failure = "throw 'INJECTED SCHEDULER FAILURE'"
    scheduler_stubs = "".join(
        (
            "function global:New-ScheduledTaskAction { param($Execute, $Argument); "
            + (failure if scheduler_failure == "action" else "[pscustomobject]@{}")
            + " }; ",
            "function global:New-ScheduledTaskTrigger { param([switch]$Daily, $At); "
            + (failure if scheduler_failure == "trigger" else "[pscustomobject]@{}")
            + " }; ",
            "function global:New-ScheduledTaskSettingsSet { param([switch]$StartWhenAvailable, $MultipleInstances); "
            + (failure if scheduler_failure == "settings" else "[pscustomobject]@{}")
            + " }; ",
            "function global:New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel); "
            + (failure if scheduler_failure == "principal" else "[pscustomobject]@{}")
            + " }; ",
            "function global:Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Principal, [switch]$Force); "
            + (failure if scheduler_failure == "register" else "$global:Registered = $true")
            + " }; ",
        )
    )
    command = (
        f"function global:wsl.exe {{ $global:LASTEXITCODE = 0; {ps_literal(wsl_runner)} }}; "
        + scheduler_stubs
        + f"& {ps_literal(str(installer))}"
        + (" -DryRun" if dry_run else "")
        + "; if ($global:Registered) { 'REGISTERED' }"
    )
    return run_powershell(command, cwd=project)


def run_wsl_git(project: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["wsl.exe", "-d", "Ubuntu", "-e", "git", "-C", translate_with_wsl(project), *args],
        capture_output=True,
        check=False,
    )


def pointer_files(main: Path, feature: Path, pages: Path | None) -> list[Path]:
    files = [feature / ".git"]
    if pages is not None:
        files.append(pages / ".git")
    files.extend(sorted(main.joinpath(".git", "worktrees").glob("*/gitdir")))
    return files


def pointer_bytes(main: Path, feature: Path, pages: Path | None) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in pointer_files(main, feature, pages)}


def overwrite_git_pointer(path: Path, content: bytes) -> None:
    if os.name == "nt":
        subprocess.run(["attrib", "-H", str(path)], capture_output=True, check=True)
    path.write_bytes(content)
    if os.name == "nt" and path.name == ".git":
        subprocess.run(["attrib", "+H", str(path)], capture_output=True, check=True)


def remove_git_pointer(path: Path) -> None:
    if os.name == "nt":
        subprocess.run(["attrib", "-H", str(path)], capture_output=True, check=True)
    path.unlink()


def test_installer_prepares_real_linked_worktrees_for_windows_and_wsl_git(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)

    before = run_wsl_git(feature, "rev-parse", "--show-toplevel")
    assert before.returncode == 128
    sentinel = feature / "do-not-touch.txt"
    sentinel.write_bytes(b"preserve me exactly\n")

    result = run_stubbed_installer(feature)

    assert result.returncode == 0, result.stderr
    assert "REGISTERED" in result.stdout
    assert sentinel.read_bytes() == b"preserve me exactly\n"
    for worktree in (feature, pages):
        for args in (("rev-parse", "--show-toplevel"), ("status", "--short"), ("worktree", "list", "--porcelain")):
            windows = run_git(*args, cwd=worktree)
            assert windows.returncode == 0, windows.stderr
            wsl = run_wsl_git(worktree, *args)
            assert wsl.returncode == 0, wsl.stderr.decode("utf-8", errors="replace")
            assert b"prunable" not in wsl.stdout

    for marker in (feature / ".git", pages / ".git"):
        line = marker.read_text(encoding="utf-8").strip()
        assert line.startswith("gitdir: ")
        assert ":/" not in line
        assert "\\" not in line
    worktree_admin = main / ".git" / "worktrees"
    for backpointer in worktree_admin.glob("*/gitdir"):
        line = backpointer.read_text(encoding="utf-8").strip()
        assert ":/" not in line
        assert "\\" not in line


def test_installer_dry_run_reports_pointer_changes_without_writing_or_registering(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    before = pointer_bytes(main, feature, pages)

    result = run_stubbed_installer(feature, dry_run=True)

    assert result.returncode == 0, result.stderr
    assert "would normalize 4 linked-worktree Git pointer file(s)" in result.stdout
    assert "no Git metadata was changed" in result.stdout
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, pages) == before


def test_installer_rejects_multiline_marker_without_changing_any_pointer(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    marker = feature / ".git"
    overwrite_git_pointer(marker, marker.read_bytes() + b"unexpected second line\n")
    before = pointer_bytes(main, feature, pages)

    result = run_stubbed_installer(feature)

    assert result.returncode != 0
    assert "exactly one gitdir line" in result.stdout + result.stderr
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, pages) == before


def test_installer_rejects_pages_pointer_from_another_repository_without_writes(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path / "expected")
    rogue_main, rogue_feature, _ = create_linked_scheduler_repo(tmp_path / "rogue", with_pages=False)
    rogue_marker = rogue_feature / ".git"
    pages_marker = pages / ".git"
    overwrite_git_pointer(pages_marker, rogue_marker.read_bytes())
    rogue_admin = next(rogue_main.joinpath(".git", "worktrees").iterdir())
    overwrite_git_pointer(rogue_admin / "gitdir", (str(pages_marker).replace("\\", "/") + "\n").encode("utf-8"))
    before = pointer_bytes(main, feature, pages)

    result = run_stubbed_installer(feature)

    assert result.returncode != 0
    assert "different Git repository" in result.stdout + result.stderr
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, pages) == before


def test_installer_rejects_wrong_backpointer_without_changing_any_pointer(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    feature_admin = next(
        path for path in main.joinpath(".git", "worktrees").iterdir() if path.name != "x-rag-pages"
    )
    overwrite_git_pointer(feature_admin / "gitdir", b"C:/not-the-feature/.git\n")
    before = pointer_bytes(main, feature, pages)

    result = run_stubbed_installer(feature)

    assert result.returncode != 0
    assert "backpointer does not reference the exact marker" in result.stdout + result.stderr
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, pages) == before


def test_installer_rejects_reparse_in_pages_path_without_writing(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    real_pages = pages.with_name("x-rag-pages-real")
    pages.rename(real_pages)
    junction = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(pages), str(real_pages)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"Could not create disposable junction: {junction.stderr}")
    before = pointer_bytes(main, feature, pages)
    try:
        result = run_stubbed_installer(feature)

        assert result.returncode != 0
        assert "reparse point" in result.stdout + result.stderr
        assert "REGISTERED" not in result.stdout
        assert pointer_bytes(main, feature, pages) == before
    finally:
        os.rmdir(pages)
        real_pages.rename(pages)


def test_installer_safely_skips_normal_repository_and_absent_pages_worktree(tmp_path: Path) -> None:
    project = create_normal_scheduler_repo(tmp_path)

    result = run_stubbed_installer(project)

    assert result.returncode == 0, result.stderr
    assert "REGISTERED" in result.stdout
    assert "0 file(s) normalized" in result.stdout
    assert (project / ".git").is_dir()
    assert not (project / ".worktrees").exists()


def test_installer_prepares_linked_project_when_pages_worktree_is_absent(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path, with_pages=False)

    result = run_stubbed_installer(feature)

    assert result.returncode == 0, result.stderr
    assert "REGISTERED" in result.stdout
    assert "2 file(s) normalized" in result.stdout
    assert not pages.exists()
    assert run_git("status", "--short", cwd=feature).returncode == 0
    assert run_wsl_git(feature, "status", "--short").returncode == 0


@pytest.mark.parametrize("failure_at", ["action", "trigger", "settings", "principal", "register"])
def test_installer_rolls_back_all_pointer_bytes_when_scheduler_registration_fails(
    tmp_path: Path,
    failure_at: str,
) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    before_bytes = pointer_bytes(main, feature, pages)
    before_wsl = {
        (worktree, args): run_wsl_git(worktree, *args).returncode
        for worktree in (feature, pages)
        for args in (("rev-parse", "--show-toplevel"), ("status", "--short"), ("worktree", "list", "--porcelain"))
    }

    result = run_stubbed_installer(feature, scheduler_failure=failure_at)

    assert result.returncode != 0
    assert "INJECTED SCHEDULER FAILURE" in result.stdout + result.stderr
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, pages) == before_bytes
    assert list(tmp_path.rglob("*.xrag-scheduler-*")) == []
    for worktree in (feature, pages):
        for args in (("rev-parse", "--show-toplevel"), ("status", "--short"), ("worktree", "list", "--porcelain")):
            assert run_git(*args, cwd=worktree).returncode == 0
            assert run_wsl_git(worktree, *args).returncode == before_wsl[(worktree, args)]


def test_installer_rejects_existing_pages_root_without_git_marker_and_preserves_sentinel(tmp_path: Path) -> None:
    require_windows_wsl_git()
    main, feature, pages = create_linked_scheduler_repo(tmp_path)
    remove_git_pointer(pages / ".git")
    sentinel = pages / "keep-me.txt"
    sentinel.write_bytes(b"do not change or delete\n")
    before = pointer_bytes(main, feature, None)

    result = run_stubbed_installer(feature)

    assert result.returncode != 0
    assert "pages .git marker" in result.stdout + result.stderr
    assert "REGISTERED" not in result.stdout
    assert pointer_bytes(main, feature, None) == before
    assert sentinel.read_bytes() == b"do not change or delete\n"
    assert not (pages / ".git").exists()


def test_readme_explains_cross_platform_pointer_scope() -> None:
    readme = PROJECT_ROOT.joinpath("README.md").read_text(encoding="utf-8")

    assert "linked-worktree Git 指针" in readme
    assert "Windows Git 与 WSL Git" in readme
    assert "相对路径" in readme
    assert "不会处理其他仓库或文件" in readme


def test_daily_runner_uses_fail_fast_dashboard_update_pipeline() -> None:
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
    assert script.count('dashboard update') == 1
    assert '"$PROJECT_ROOT/.venv/bin/xrag" --root "$PROJECT_ROOT" dashboard update --no-publish' in script
    assert "command -v python.exe" in script
    assert "command -v wslpath" in script
    assert "sys.platform" in script
    assert "sys.version_info" in script
    assert "readlink -f" in script
    assert "MZ" in script
    assert 'wslpath -w "$PROJECT_ROOT/scripts/publish-dashboard.py"' in script
    assert 'exec "$WINDOWS_PYTHON" -I -S "$WINDOWS_WRAPPER"' in script
    assert "--root" not in script.split("dashboard update --no-publish", 1)[1]
    assert 'collect --all' not in script


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


def test_scheduled_action_uses_direct_exec_and_preserves_literal_path(tmp_path: Path) -> None:
    project = create_normal_scheduler_repo(tmp_path)
    installer = project / "scripts" / INSTALLER.name
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
        f"& {ps_literal(str(installer))}; "
        "if (-not $global:ActionOk) { throw 'action stub was not called' }; "
        "if (-not $global:Registered) { throw 'register stub was not called' }; "
        "'ACTION_OK'; 'REGISTERED'"
    )

    result = run_powershell(command, cwd=project)

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
    combined = (
        RUNNER.read_text(encoding="utf-8")
        + PUBLISH_WRAPPER.read_text(encoding="utf-8")
        + INSTALLER.read_text(encoding="utf-8")
    ).lower()

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


def test_runtime_media_directory_is_ignored_by_git() -> None:
    patterns = PROJECT_ROOT.joinpath(".gitignore").read_text(encoding="utf-8").splitlines()

    assert "data/media/" in patterns


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


def prepare_wsl_runner(
    tmp_path: Path,
    *,
    with_opencli: bool,
    with_python: bool = True,
    fake_linux_python: bool = False,
) -> tuple[Path, str, str, str]:
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
    (scripts / "publish-dashboard.py").write_text(
        "import os, sys\n"
        "for value in sys.argv:\n"
        "    print(f'PYTHON_ARG=<{value}>')\n"
        "print('PYTHON_RAN')\n"
        "raise SystemExit(int(os.environ.get('FAKE_PYTHON_STATUS', '0')))\n",
        encoding="utf-8",
        newline="\n",
    )
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
    if fake_linux_python:
        fake_python = local_bin / "python.exe"
        fake_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'win32\\n3\\n14\\n'\n",
            encoding="utf-8",
            newline="\n",
        )
        fake_python.chmod(0o755)

    windows_python = ""
    if with_python and not fake_linux_python:
        if os.name == "nt":
            windows_python = translate_with_wsl(Path(sys.executable))
        else:
            discovered = shutil.which("python.exe")
            if discovered is None:
                pytest.skip("Windows Python is unavailable through WSL interop")
            windows_python = str(Path(discovered).resolve())

    return (
        project,
        translate_with_wsl(scripts / "run-daily.sh"),
        translate_with_wsl(fake_home),
        str(PurePosixPath(windows_python).parent) if windows_python else "",
    )


def run_daily_in_minimal_environment(
    wsl_runner: str,
    wsl_home: str,
    windows_python_dir: str,
    status: int = 0,
    python_status: int = 0,
) -> subprocess.CompletedProcess[bytes]:
    search_path = f"{wsl_home}/.local/bin"
    if windows_python_dir:
        search_path += f":{windows_python_dir}"
    search_path += ":/usr/bin:/bin"
    if os.name != "nt":
        return subprocess.run(
            ["env", "-i", f"HOME={wsl_home}", f"PATH={search_path}", f"FAKE_STATUS={status}", f"FAKE_PYTHON_STATUS={python_status}", "WSLENV=FAKE_PYTHON_STATUS", "bash", wsl_runner],
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
            f"PATH={search_path}",
            f"FAKE_STATUS={status}",
            f"FAKE_PYTHON_STATUS={python_status}",
            "WSLENV=FAKE_PYTHON_STATUS",
            "bash",
            wsl_runner,
        ],
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-path orchestration")
def test_daily_runner_finds_opencli_and_sets_offline_environment(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path, with_opencli=True
    )

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home, python_dir)

    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    wsl_project = str(PurePosixPath(wsl_runner).parents[1])
    assert "scheduled collection start" in log
    assert [line for line in log.splitlines() if line.startswith("ARG=")] == [
        "ARG=<--root>",
        f"ARG=<{wsl_project}>",
        "ARG=<dashboard>",
        "ARG=<update>",
        "ARG=<--no-publish>",
    ]
    assert f"OPENCLI=<{wsl_home}/.local/bin/opencli>" in log
    assert "HF_HUB_OFFLINE=<1>" in log
    assert "TRANSFORMERS_OFFLINE=<1>" in log
    python_args = [line for line in log.splitlines() if line.startswith("PYTHON_ARG=")]
    assert python_args[0].endswith("publish-dashboard.py>")
    assert len(python_args) == 1
    assert "PYTHON_RAN" in log


def test_daily_runner_exits_127_before_xrag_when_opencli_is_absent(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path, with_opencli=False
    )

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home, python_dir)

    assert result.returncode == 127, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "scheduled collection start" in log
    assert "opencli" in log.lower()
    assert "not found" in log.lower()
    assert "XRAG_RAN" not in log


def test_daily_runner_propagates_xrag_nonzero_status(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path, with_opencli=True
    )

    result = run_daily_in_minimal_environment(
        wsl_runner, wsl_home, python_dir, status=23
    )

    assert result.returncode == 23, result.stderr.decode("utf-8", errors="replace")
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "XRAG_RAN" in log
    assert "PYTHON_RAN" not in log


def test_daily_runner_exits_127_before_xrag_when_windows_python_is_absent(
    tmp_path: Path,
) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path, with_opencli=True, with_python=False
    )

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home, python_dir)

    assert result.returncode == 127
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "python.exe" in log
    assert "XRAG_RAN" not in log
    assert "PYTHON_RAN" not in log


@pytest.mark.skipif(os.name != "nt", reason="Windows drive-path orchestration")
def test_daily_runner_propagates_native_publisher_status(tmp_path: Path) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path, with_opencli=True
    )

    result = run_daily_in_minimal_environment(
        wsl_runner, wsl_home, python_dir, python_status=29
    )

    assert result.returncode == 29
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "XRAG_RAN" in log
    assert "PYTHON_RAN" in log


def test_daily_runner_rejects_spoofed_linux_python_before_xrag(
    tmp_path: Path,
) -> None:
    project, wsl_runner, wsl_home, python_dir = prepare_wsl_runner(
        tmp_path,
        with_opencli=True,
        with_python=False,
        fake_linux_python=True,
    )

    result = run_daily_in_minimal_environment(wsl_runner, wsl_home, python_dir)

    assert result.returncode == 127
    log = (project / "logs" / "scheduler.log").read_text(encoding="utf-8")
    assert "Windows python.exe" in log
    assert "XRAG_RAN" not in log
    assert "PYTHON_RAN" not in log


def _assert_generic_wrapper_failure(
    result: subprocess.CompletedProcess[str], *forbidden: str
) -> None:
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == "Error: dashboard publication failed\n"
    assert "Traceback" not in result.stdout + result.stderr
    for value in forbidden:
        assert value not in result.stdout + result.stderr


def test_publish_wrapper_rejects_extra_arguments_without_echoing_them() -> None:
    secret = "auth_token=wrapper-extra-secret"

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(PUBLISH_WRAPPER), secret],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, secret, str(PROJECT_ROOT))


def test_publish_wrapper_never_adds_project_code_to_sys_path() -> None:
    source = PUBLISH_WRAPPER.read_text(encoding="utf-8")

    assert "sys.path.insert" not in source
    assert "sys.path.append" not in source


def test_source_loader_executes_captured_bytes_after_path_replacement(
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "xrag_publish_wrapper_test", PUBLISH_WRAPPER
    )
    assert spec is not None and spec.loader is not None
    wrapper_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper_module)
    source_path = tmp_path / "captured.py"
    trusted = b"VALUE = 'trusted-head-blob'\n"
    marker = tmp_path / "replacement-executed"
    source_path.write_bytes(trusted)
    captured = source_path.read_bytes()
    source_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
        "VALUE = 'malicious-worktree'\n",
        encoding="utf-8",
    )
    module_name = "xrag_captured_source_test"
    try:
        loaded = wrapper_module._load_source_module(
            module_name,
            source_path,
            captured,
            package="",
        )
    finally:
        sys.modules.pop(module_name, None)

    assert loaded.VALUE == "trusted-head-blob"
    assert not marker.exists()


def test_copied_wrapper_rejects_fake_module_before_import_without_git(
    tmp_path: Path,
) -> None:
    root = tmp_path / "untrusted-copy"
    wrapper = root / "scripts" / PUBLISH_WRAPPER.name
    package = root / "src" / "xrag"
    package.mkdir(parents=True)
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(PUBLISH_WRAPPER.read_bytes())
    marker = tmp_path / "fake-module-executed"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "dashboard_publish.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_MARKER']).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(wrapper)],
        env={**os.environ, "FAKE_MARKER": str(marker)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, str(root), str(marker))
    assert not marker.exists()


def create_trusted_publisher_repo(
    tmp_path: Path,
    *,
    publisher_source: str | None = None,
) -> tuple[Path, Path]:
    root = tmp_path / "trusted Windows project"
    remote = tmp_path / "origin.git"
    scripts = root / "scripts"
    package = root / "src" / "xrag"
    scripts.mkdir(parents=True)
    package.mkdir(parents=True)
    remote.mkdir()
    shutil.copy2(PUBLISH_WRAPPER, scripts / PUBLISH_WRAPPER.name)
    shutil.copy2(PROJECT_ROOT / "src" / "xrag" / "__init__.py", package / "__init__.py")
    if publisher_source is None:
        shutil.copy2(
            PROJECT_ROOT / "src" / "xrag" / "dashboard_publish.py",
            package / "dashboard_publish.py",
        )
    else:
        (package / "dashboard_publish.py").write_text(
            publisher_source, encoding="utf-8"
        )
    public_content = PROJECT_ROOT / "src" / "xrag" / "public_content.py"
    if public_content.exists():
        shutil.copy2(public_content, package / "public_content.py")
    else:
        (package / "public_content.py").write_text("", encoding="utf-8")

    assert run_git("init", "--bare", cwd=remote).returncode == 0
    assert run_git("init", "-b", "main", cwd=root).returncode == 0
    assert run_git("config", "user.name", "Wrapper Test", cwd=root).returncode == 0
    assert run_git("config", "user.email", "wrapper@example.invalid", cwd=root).returncode == 0
    assert run_git("remote", "add", "origin", str(remote), cwd=root).returncode == 0
    assert run_git("add", "scripts", "src", cwd=root).returncode == 0
    assert run_git("commit", "-m", "trusted publisher code", cwd=root).returncode == 0
    return root, remote


def prepare_dashboard_site(root: Path) -> None:
    site = root / "data" / "dashboard-site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text(
        "<!doctype html><title>Dashboard</title>", encoding="utf-8"
    )
    (site / ".nojekyll").write_text("", encoding="utf-8")
    (site / "data" / "latest.json").write_text("{}", encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="Windows Python security boundary")
def test_trusted_wrapper_requires_isolated_no_site_python(tmp_path: Path) -> None:
    root, _ = create_trusted_publisher_repo(tmp_path)
    prepare_dashboard_site(root)

    result = subprocess.run(
        [sys.executable, "-S", str(root / "scripts" / PUBLISH_WRAPPER.name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, str(root))


@pytest.mark.skipif(os.name != "nt", reason="Windows Python security boundary")
@pytest.mark.parametrize("shadow_name", ["json.py", "pathlib.py", "subprocess.py"])
def test_isolated_wrapper_ignores_untracked_stdlib_shadows(
    tmp_path: Path,
    shadow_name: str,
) -> None:
    root, _ = create_trusted_publisher_repo(tmp_path)
    prepare_dashboard_site(root)
    marker = tmp_path / f"{shadow_name}.executed"
    (root / "scripts" / shadow_name).write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_MARKER']).write_text('executed', encoding='utf-8')\n"
        "raise RuntimeError('auth_token=stdlib-shadow-secret')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(root / "scripts" / PUBLISH_WRAPPER.name),
        ],
        env={**os.environ, "FAKE_MARKER": str(marker)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "stdlib-shadow-secret" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Python security boundary")
@pytest.mark.parametrize("index_flag", ["--assume-unchanged", "--skip-worktree"])
def test_wrapper_never_executes_flag_hidden_worktree_module(
    tmp_path: Path,
    index_flag: str,
) -> None:
    root, _ = create_trusted_publisher_repo(tmp_path)
    marker = tmp_path / "hidden-module-executed"
    module = root / "src" / "xrag" / "dashboard_publish.py"
    module.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_MARKER']).write_text('executed', encoding='utf-8')\n"
        "class PagesPublisher:\n"
        "    def __init__(self, *args): pass\n"
        "    def publish(self, path): return {'changed': False}\n",
        encoding="utf-8",
    )
    relative = "src/xrag/dashboard_publish.py"
    assert run_git("update-index", index_flag, "--", relative, cwd=root).returncode == 0

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(root / "scripts" / PUBLISH_WRAPPER.name),
        ],
        env={**os.environ, "FAKE_MARKER": str(marker)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, str(root), str(marker))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Python security boundary")
@pytest.mark.parametrize(
    "publisher_source,secret",
    [
        ("raise KeyError('auth_token=key-error-secret')\n", "key-error-secret"),
        ("raise ImportError('ct0=import-error-secret')\n", "import-error-secret"),
        ("raise SystemExit('api_key=exit-secret')\n", "exit-secret"),
    ],
)
def test_trusted_wrapper_redacts_all_import_failures(
    tmp_path: Path,
    publisher_source: str,
    secret: str,
) -> None:
    root, _ = create_trusted_publisher_repo(
        tmp_path, publisher_source=publisher_source
    )

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(root / "scripts" / PUBLISH_WRAPPER.name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, secret, str(root))


@pytest.mark.skipif(os.name != "nt", reason="Windows Python security boundary")
def test_trusted_wrapper_rejects_modified_module_before_execution(
    tmp_path: Path,
) -> None:
    root, _ = create_trusted_publisher_repo(
        tmp_path,
        publisher_source="class PagesPublisher:\n    pass\n",
    )
    marker = tmp_path / "modified-module-executed"
    module = root / "src" / "xrag" / "dashboard_publish.py"
    module.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['FAKE_MARKER']).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(root / "scripts" / PUBLISH_WRAPPER.name)],
        env={**os.environ, "FAKE_MARKER": str(marker)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    _assert_generic_wrapper_failure(result, str(root), str(marker))
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows Git integration proof")
def test_windows_python_wrapper_publishes_to_disposable_local_remote(
    tmp_path: Path,
) -> None:
    root, _ = create_trusted_publisher_repo(tmp_path)
    prepare_dashboard_site(root)

    result = subprocess.run(
        [sys.executable, "-I", "-S", str(root / "scripts" / PUBLISH_WRAPPER.name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"changed": True, "branch": "gh-pages"}
    assert run_git("show", "gh-pages:index.html", cwd=root).stdout == (
        "<!doctype html><title>Dashboard</title>"
    )
    assert run_git("ls-remote", "--exit-code", "--heads", "origin", "refs/heads/gh-pages", cwd=root).returncode == 0


def test_readme_documents_hybrid_scheduler_runtime() -> None:
    readme = PROJECT_ROOT.joinpath("README.md").read_text(encoding="utf-8")

    assert "WSL performs collection and build" in readme
    assert "Windows Python, Windows Git, and Git Credential Manager" in readme
    assert "--no-publish" in readme
    assert "Manual `dashboard update` still publishes by default" in readme
    assert "Python 3.11" in readme
    assert "PyYAML" in readme
    assert "python.exe scripts/publish-dashboard.py --root" not in readme
    assert "python.exe -I -S scripts/publish-dashboard.py" in readme
    assert "-I -S" in readme
    assert "HEAD tree blobs" in readme
    assert "minimal trust boundary" in readme
