from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.dashboard_publish import PagesPublisher


NOW = datetime(2026, 8, 11, 3, 4, 5, tzinfo=timezone.utc)
TEXT_FILES = {
    "index.html": "<!doctype html><script src='assets/app.js'></script>\n",
    ".nojekyll": "",
    "assets/styles.css": "body { color: #123; }\n",
    "assets/app.js": "fetch('data/latest.json');\n",
    "data/latest.json": '{"version": 1}\n',
    "data/2026-08-11.json": '{"version": 1}\n',
}
PNG = b"\x89PNG\r\n\x1a\npayload"


class FakeRunner:
    def __init__(
        self,
        root: Path,
        worktree: Path,
        *,
        local_branch: bool = True,
        remote_code: int = 2,
        diff_code: int = 1,
        top_level: Path | None = None,
        branch: str = "gh-pages",
        status: str = "",
        staged: str | None = None,
        failures: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
    ) -> None:
        self.root = root
        self.worktree = worktree
        self.local_branch = local_branch
        self.remote_code = remote_code
        self.diff_code = diff_code
        self.top_level = top_level or worktree
        self.branch = branch
        self.status = status
        self.staged = staged
        self.failures = failures or {}
        self.calls: list[tuple[list[str], Path, str | None]] = []

    def __call__(
        self, command: list[str], cwd: Path, input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), Path(cwd), input_text))
        key = tuple(command)
        if key in self.failures:
            code, stdout, stderr = self.failures[key]
            return subprocess.CompletedProcess(command, code, stdout, stderr)
        if command[:3] == ["git", "show-ref", "--verify"]:
            return self._result(command, 0 if self.local_branch else 1)
        if command[:2] == ["git", "ls-remote"]:
            output = "abc\trefs/heads/gh-pages\n" if self.remote_code == 0 else ""
            return self._result(command, self.remote_code, output)
        if command[:2] == ["git", "mktree"]:
            return self._result(command, 0, "empty-tree\n")
        if command[:2] == ["git", "commit-tree"]:
            return self._result(command, 0, "initial-commit\n")
        if command[:3] == ["git", "worktree", "add"]:
            self.worktree.mkdir(parents=True, exist_ok=False)
            self.worktree.joinpath(".git").write_text(
                "gitdir: fake\n", encoding="utf-8"
            )
            return self._result(command)
        if command[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return self._result(command, 0, f"{self.top_level}\n")
        if command[:2] == ["git", "status"]:
            return self._result(command, 0, self.status)
        if command[:2] == ["git", "symbolic-ref"]:
            return self._result(command, 0, f"{self.branch}\n")
        if command[:4] == ["git", "diff", "--cached", "--name-only"]:
            names = self.staged
            if names is None:
                names = "\0".join(TEXT_FILES) + "\0assets/media/chart.png\0"
            return self._result(command, 0, names)
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return self._result(command, self.diff_code)
        return self._result(command)

    @staticmethod
    def _result(
        command: list[str], code: int = 0, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    def commands(self) -> list[list[str]]:
        return [command for command, _, _ in self.calls]


def prepare_site(root: Path) -> Path:
    site = root / "data" / "dashboard-site"
    for relative, content in TEXT_FILES.items():
        path = site / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    image = site / "assets" / "media" / "chart.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(PNG)
    return site


def prepare_existing_worktree(root: Path) -> Path:
    worktree = root / ".worktrees" / "x-rag-pages"
    worktree.mkdir(parents=True)
    worktree.joinpath(".git").write_text("gitdir: fake\n", encoding="utf-8")
    return worktree


def publisher(
    root: Path, worktree: Path, runner: FakeRunner
) -> PagesPublisher:
    return PagesPublisher(root, worktree, runner=runner, clock=lambda: NOW)


def command_index(runner: FakeRunner, prefix: list[str]) -> int:
    return next(
        index
        for index, command in enumerate(runner.commands())
        if command[: len(prefix)] == prefix
    )


def assert_no_prohibited_commands(runner: FakeRunner) -> None:
    prohibited = {"rm", "clean", "reset", "checkout", "restore"}
    for command in runner.commands():
        assert command[0] == "git"
        assert not any(part in prohibited for part in command[1:])


def test_existing_clean_worktree_copies_allowlist_commits_and_pushes(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    unexpected = site / "debug.tmp"
    unexpected.write_text("ignored", encoding="utf-8")
    runner = FakeRunner(tmp_path, worktree)

    result = publisher(tmp_path, worktree, runner).publish(site)

    assert result == {"changed": True, "branch": "gh-pages"}
    for relative, content in TEXT_FILES.items():
        assert (worktree / relative).read_text(encoding="utf-8") == content
    assert (worktree / "assets" / "media" / "chart.png").read_bytes() == PNG
    assert not (worktree / "debug.tmp").exists()
    assert [
        "git",
        "add",
        "--",
        ".nojekyll",
        "index.html",
        "assets",
        "data",
    ] in runner.commands()
    assert [
        "git",
        "commit",
        "-m",
        "data: publish dashboard 2026-08-11T03:04:05+00:00",
    ] in runner.commands()
    assert ["git", "push", "origin", "gh-pages"] in runner.commands()
    assert command_index(runner, ["git", "add"]) < command_index(
        runner, ["git", "commit"]
    )
    assert command_index(runner, ["git", "commit"]) < command_index(
        runner, ["git", "push"]
    )
    assert_no_prohibited_commands(runner)


def test_unchanged_site_skips_commit_and_push(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree, diff_code=0)

    result = publisher(tmp_path, worktree, runner).publish(site)

    assert result == {"changed": False, "branch": "gh-pages"}
    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())
    assert not any(command[:2] == ["git", "push"] for command in runner.commands())


def test_absent_worktree_uses_existing_local_branch(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    runner = FakeRunner(tmp_path, worktree, local_branch=True, diff_code=0)

    publisher(tmp_path, worktree, runner).publish(site)

    assert [
        "git",
        "show-ref",
        "--verify",
        "--quiet",
        "refs/heads/gh-pages",
    ] in runner.commands()
    assert ["git", "worktree", "add", str(worktree), "gh-pages"] in runner.commands()
    assert not any(command[:2] == ["git", "ls-remote"] for command in runner.commands())


def test_absent_worktree_fetches_existing_remote_branch(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    runner = FakeRunner(
        tmp_path, worktree, local_branch=False, remote_code=0, diff_code=0
    )

    publisher(tmp_path, worktree, runner).publish(site)

    commands = runner.commands()
    assert [
        "git",
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        "refs/heads/gh-pages",
    ] in commands
    assert [
        "git",
        "fetch",
        "origin",
        "refs/heads/gh-pages:refs/remotes/origin/gh-pages",
    ] in commands
    assert ["git", "branch", "--track", "gh-pages", "origin/gh-pages"] in commands
    assert command_index(runner, ["git", "fetch"]) < command_index(
        runner, ["git", "worktree", "add"]
    )


def test_absent_remote_branch_is_initialized_from_empty_tree(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    runner = FakeRunner(
        tmp_path, worktree, local_branch=False, remote_code=2, diff_code=0
    )

    publisher(tmp_path, worktree, runner).publish(site)

    assert ["git", "mktree"] in runner.commands()
    mktree_call = next(call for call in runner.calls if call[0] == ["git", "mktree"])
    assert mktree_call[2] == ""
    assert [
        "git",
        "commit-tree",
        "empty-tree",
        "-m",
        "chore: initialize dashboard pages",
    ] in runner.commands()
    assert ["git", "branch", "gh-pages", "initial-commit"] in runner.commands()
    assert_no_prohibited_commands(runner)


@pytest.mark.parametrize("remote_code", [1, 3, 128])
def test_remote_query_error_aborts_without_creating_worktree(
    tmp_path: Path, remote_code: int
) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    runner = FakeRunner(tmp_path, worktree, local_branch=False, remote_code=remote_code)

    with pytest.raises(RuntimeError, match="git ls-remote failed"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not worktree.exists()
    assert not any(command[:2] == ["git", "mktree"] for command in runner.commands())


@pytest.mark.parametrize(
    ("top_level", "branch", "status", "message"),
    [
        ("other", "gh-pages", "", "top-level"),
        (None, "main", "", "branch"),
        (None, "gh-pages", " M index.html\n", "clean"),
        (None, "gh-pages", "?? notes.txt\n", "clean"),
    ],
)
def test_existing_worktree_must_be_exact_clean_gh_pages(
    tmp_path: Path,
    top_level: str | None,
    branch: str,
    status: str,
    message: str,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    actual_top = tmp_path / top_level if top_level else worktree
    runner = FakeRunner(
        tmp_path, worktree, top_level=actual_top, branch=branch, status=status
    )

    with pytest.raises(RuntimeError, match=message):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "add"] for command in runner.commands())


@pytest.mark.parametrize(
    "site_factory",
    [
        lambda root: root / "other-site",
        lambda root: root / "data",
    ],
)
def test_source_must_resolve_to_exact_dashboard_site(
    tmp_path: Path, site_factory: Callable[[Path], Path]
) -> None:
    expected = prepare_site(tmp_path)
    site = site_factory(tmp_path)
    site.mkdir(parents=True, exist_ok=True)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []
    assert expected.joinpath("index.html").exists()


@pytest.mark.parametrize("missing", ["index.html", ".nojekyll", "data/latest.json"])
def test_required_source_files_must_exist(tmp_path: Path, missing: str) -> None:
    site = prepare_site(tmp_path)
    (site / missing).unlink()
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("relative", "content", "message"),
    [
        ("assets/app.js", b"const password = 'secret';\n", "unsafe public output"),
        ("assets/styles.css", b"\xff\xfe", "UTF-8"),
        ("data/latest.json", b"{bad json", "JSON"),
        ("data/2026-08-11.json", b"[] trailing", "JSON"),
    ],
)
def test_all_text_is_safe_utf8_and_json_is_parseable_before_git_or_writes(
    tmp_path: Path, relative: str, content: bytes, message: str
) -> None:
    site = prepare_site(tmp_path)
    (site / relative).write_bytes(content)
    worktree = prepare_existing_worktree(tmp_path)
    sentinel = worktree / "index.html"
    sentinel.write_text("old", encoding="utf-8")
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match=message):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []
    assert sentinel.read_text(encoding="utf-8") == "old"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("bad.jpg", b"GIF89a payload"),
        ("bad.jpeg", b"not jpeg"),
        ("bad.png", b"not png"),
        ("bad.gif", b"not gif"),
        ("bad.webp", b"RIFFxxxxNOPEpayload"),
    ],
)
def test_invalid_image_signature_aborts_before_git(
    tmp_path: Path, name: str, content: bytes
) -> None:
    site = prepare_site(tmp_path)
    (site / "assets" / name).write_bytes(content)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="image"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_unexpected_regular_extensions_are_ignored(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    site.joinpath("assets", "source.map").write_text(
        "private map", encoding="utf-8"
    )
    site.joinpath("data", "notes.txt").write_text("private note", encoding="utf-8")
    site.joinpath("tests").mkdir()
    site.joinpath("tests", "fixture.json").write_text(
        '{"fixture": true}', encoding="utf-8"
    )
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree, diff_code=0)

    publisher(tmp_path, worktree, runner).publish(site)

    assert not worktree.joinpath("assets", "source.map").exists()
    assert not worktree.joinpath("data", "notes.txt").exists()
    assert not worktree.joinpath("tests").exists()


def test_unicode_allowlisted_path_is_validated_without_git_quoting(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    unicode_asset = site / "assets" / "设计.js"
    unicode_asset.write_text("const chart = true;\n", encoding="utf-8")
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(
        tmp_path,
        worktree,
        diff_code=0,
        staged="index.html\0assets/设计.js\0",
    )

    publisher(tmp_path, worktree, runner).publish(site)

    assert (worktree / "assets" / "设计.js").read_text(encoding="utf-8") == (
        "const chart = true;\n"
    )
    assert ["git", "diff", "--cached", "--name-only", "-z"] in runner.commands()


def test_source_symlink_anywhere_is_rejected_without_reading_target(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("const password = 'external';", encoding="utf-8")
    link = site / "ignored.tmp"
    try:
        os.symlink(outside, link)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_source_directory_symlink_is_rejected(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    outside = tmp_path / "external-assets"
    outside.mkdir()
    site.joinpath("assets", "media", "chart.png").unlink()
    site.joinpath("assets", "media").rmdir()
    try:
        os.symlink(outside, site / "assets" / "media", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_source_root_symlink_is_rejected(tmp_path: Path) -> None:
    real_site = prepare_site(tmp_path)
    external = tmp_path / "external-site"
    real_site.rename(external)
    try:
        os.symlink(external, real_site, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(real_site)

    assert runner.calls == []


def test_worktree_outside_root_is_rejected_before_git(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path.parent / f"{tmp_path.name}-outside-pages"
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="worktree"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_existing_worktree_symlink_is_rejected(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    external = tmp_path / "external-worktree"
    external.mkdir()
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    worktree.parent.mkdir()
    try:
        os.symlink(external, worktree, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="worktree"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_target_symlink_is_rejected_without_touching_external_file(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    external = tmp_path / "external.html"
    external.write_text("sentinel", encoding="utf-8")
    try:
        os.symlink(external, worktree / "index.html")
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="target"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert external.read_text(encoding="utf-8") == "sentinel"
    assert not any(command[:2] == ["git", "add"] for command in runner.commands())


def test_worktree_is_revalidated_before_each_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.dashboard_publish as module

    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    parked = tmp_path / "parked-worktree"
    external = tmp_path / "external-target"
    external.mkdir()
    runner = FakeRunner(tmp_path, worktree)
    original_replace = module.os.replace
    swapped = False

    def replace_then_swap(source: object, destination: object) -> None:
        nonlocal swapped
        original_replace(source, destination)  # type: ignore[arg-type]
        if not swapped and Path(destination) == worktree / ".nojekyll":
            worktree.rename(parked)
            try:
                os.symlink(external, worktree, target_is_directory=True)
            except OSError as error:
                parked.rename(worktree)
                pytest.skip(f"symlinks unavailable: {error}")
            swapped = True

    monkeypatch.setattr(module.os, "replace", replace_then_swap)
    try:
        with pytest.raises(ValueError, match="target"):
            publisher(tmp_path, worktree, runner).publish(site)
        assert list(external.iterdir()) == []
    finally:
        if swapped:
            worktree.unlink()
            parked.rename(worktree)


@pytest.mark.parametrize(
    "unexpected",
    ["private.txt", "assets/source.map", "assets\\escaped.js", "data/../private.json"],
)
def test_unexpected_staged_path_is_rejected_before_commit(
    tmp_path: Path, unexpected: str
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(
        tmp_path,
        worktree,
        staged=f"index.html\0assets/app.js\0{unexpected}\0",
    )

    with pytest.raises(RuntimeError, match="staged path"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())
    assert not any(command[:2] == ["git", "push"] for command in runner.commands())


@pytest.mark.parametrize(
    ("failing_command", "error_line", "push_expected"),
    [
        (
            (
                "git",
                "commit",
                "-m",
                "data: publish dashboard 2026-08-11T03:04:05+00:00",
            ),
            "commit denied",
            False,
        ),
        (("git", "push", "origin", "gh-pages"), "push denied", True),
    ],
)
def test_commit_and_push_failures_do_not_claim_success_or_dump_multiline_errors(
    tmp_path: Path,
    failing_command: tuple[str, ...],
    error_line: str,
    push_expected: bool,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(
        tmp_path,
        worktree,
        failures={failing_command: (1, "", f"{error_line}\nsecret second line\n")},
    )

    with pytest.raises(RuntimeError) as caught:
        publisher(tmp_path, worktree, runner).publish(site)

    assert error_line in str(caught.value)
    assert "secret second line" not in str(caught.value)
    assert (
        any(command[:2] == ["git", "push"] for command in runner.commands())
        is push_expected
    )


def test_unexpected_cached_diff_exit_code_raises(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree, diff_code=2)

    with pytest.raises(RuntimeError, match="git diff"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("kind", ["source", "worktree", "target-parent"])
def test_windows_junctions_are_rejected(tmp_path: Path, kind: str) -> None:
    if not hasattr(Path, "is_junction"):
        pytest.skip("Path.is_junction unavailable")
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    external = tmp_path / f"external-{kind}"
    external.mkdir()
    if kind == "source":
        target = site / "ignored-dir"
    elif kind == "worktree":
        worktree.joinpath(".git").unlink()
        worktree.rmdir()
        target = worktree
    else:
        target = worktree / "assets"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(target), str(external)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junctions unavailable: {result.stderr or result.stdout}")
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError):
        publisher(tmp_path, worktree, runner).publish(site)


def test_default_runner_uses_text_utf8_capture_and_no_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.dashboard_publish as module

    observed: dict[str, object] = {}

    def fake_run(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._default_runner(["git", "status"], tmp_path, "input")

    assert result.returncode == 0
    assert observed == {
        "cwd": tmp_path,
        "input": "input",
        "text": True,
        "encoding": "utf-8",
        "capture_output": True,
        "check": False,
    }
