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
        remote_code: int = 0,
        local_head: str = "a" * 40,
        remote_head: str | None = None,
        diff_code: int = 1,
        top_level: Path | None = None,
        branch: str = "gh-pages",
        status: str = "",
        staged: str | None = None,
        failures: dict[tuple[str, ...], tuple[int, str, str]] | None = None,
        index_oid_override: str | None = None,
    ) -> None:
        self.root = root
        self.worktree = worktree
        self.local_branch = local_branch
        self.remote_code = remote_code
        self.local_head = local_head
        self.remote_head = remote_head or local_head
        self.diff_code = diff_code
        self.top_level = top_level or worktree
        self.branch = branch
        self.status = status
        self.staged = staged
        self.failures = failures or {}
        self.index_oid_override = index_oid_override
        self.prepared_content = fake_prepared_content(root)
        self.calls: list[tuple[list[str], Path, str | bytes | None]] = []

    def __call__(
        self, command: list[str], cwd: Path, input_text: str | bytes | None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), Path(cwd), input_text))
        key = tuple(command)
        if key in self.failures:
            code, stdout, stderr = self.failures[key]
            return subprocess.CompletedProcess(command, code, stdout, stderr)
        if command[:3] == ["git", "show-ref", "--verify"]:
            return self._result(command, 0 if self.local_branch else 1)
        if command[:2] == ["git", "ls-remote"]:
            output = (
                f"{self.remote_head}\trefs/heads/gh-pages\n"
                if self.remote_code == 0
                else ""
            )
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
            top = self.root if cwd == self.root else self.top_level
            return self._result(command, 0, f"{top}\n")
        if command[:3] == ["git", "rev-parse", "--git-common-dir"]:
            return self._result(command, 0, f"{self.root / '.git'}\n")
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return self._result(command, 0, f"{self.local_head}\n")
        if command[:3] == ["git", "rev-parse", "--show-object-format"]:
            return self._result(command, 0, "sha1\n")
        if command[:3] == ["git", "worktree", "list"]:
            return self._result(
                command,
                0,
                f"worktree {self.root}\0HEAD {'b' * 40}\0branch refs/heads/main\0\0"
                f"worktree {self.worktree}\0HEAD {self.local_head}\0"
                "branch refs/heads/gh-pages\0\0",
            )
        if command[:2] == ["git", "status"]:
            return self._result(command, 0, self.status)
        if command[:2] == ["git", "symbolic-ref"]:
            return self._result(command, 0, f"{self.branch}\n")
        if command[:4] == ["git", "diff", "--cached", "--name-only"]:
            names = self.staged
            if names is None:
                paths = list(self.prepared_content)
                names = "" if self.diff_code == 0 else "\0".join(paths) + "\0"
            return self._result(command, 0, names)
        if command[:3] == ["git", "ls-files", "--stage"]:
            entries = "".join(
                "100644 "
                f"{self.index_oid_override or fake_blob_oid(content)} 0\t{path}\0"
                for path, content in self.prepared_content.items()
            )
            return self._result(command, 0, entries)
        if command[:3] == ["git", "ls-tree", "-r"]:
            entries = ""
            if self.diff_code == 0:
                entries = "".join(
                    f"100644 blob {fake_blob_oid(content)}\t{path}\0"
                    for path, content in self.prepared_content.items()
                )
            return self._result(command, 0, entries)
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return self._result(command, self.diff_code)
        if command == [
            "git",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.safecrlf=false",
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ]:
            expected = "".join(
                f":(literal){path}\0" for path in self.prepared_content
            )
            if input_text != expected:
                raise AssertionError("unexpected fake Git add pathspec input")
            return self._result(command)
        if command == [
            "git",
            "fetch",
            "origin",
            "+refs/heads/gh-pages:refs/remotes/origin/gh-pages",
        ]:
            return self._result(command)
        if command in (
            ["git", "branch", "--track", "gh-pages", "origin/gh-pages"],
            ["git", "branch", "gh-pages", "initial-commit"],
        ):
            return self._result(command)
        if command[:3] == ["git", "commit", "-m"] and len(command) == 4:
            return self._result(command)
        if command == ["git", "push", "origin", "gh-pages"]:
            return self._result(command)
        if command in (
            ["git", "var", "GIT_AUTHOR_IDENT"],
            ["git", "var", "GIT_COMMITTER_IDENT"],
        ):
            return self._result(
                command,
                0,
                "Publisher <publisher@example.invalid> 0 +0000\n",
            )
        raise AssertionError(f"unexpected fake Git command: {command!r}")

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


def fake_prepared_content(root: Path) -> dict[str, bytes]:
    site = root / "data" / "dashboard-site"
    result: dict[str, bytes] = {}
    for path in site.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(site).as_posix()
        suffix = path.suffix.lower()
        if relative in {"index.html", ".nojekyll"}:
            result[relative] = path.read_bytes()
        elif relative.startswith("assets/") and suffix in {
            ".css",
            ".js",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
        }:
            result[relative] = path.read_bytes()
        elif relative.startswith("data/") and suffix == ".json":
            result[relative] = path.read_bytes()
    return dict(sorted(result.items()))


def fake_prepared_paths(root: Path) -> list[str]:
    return list(fake_prepared_content(root))


def fake_blob_oid(content: bytes) -> str:
    import hashlib

    framed = f"blob {len(content)}\0".encode("ascii") + content
    return hashlib.sha1(framed).hexdigest()


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


def has_git_subcommand(command: list[str], subcommand: str) -> bool:
    index = 1
    while index < len(command) and command[index] == "-c":
        index += 2
    return index < len(command) and command[index] == subcommand


def git_subcommand_index(runner: FakeRunner, subcommand: str) -> int:
    return next(
        index
        for index, command in enumerate(runner.commands())
        if has_git_subcommand(command, subcommand)
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
        "-c",
        "core.autocrlf=false",
        "-c",
        "core.safecrlf=false",
        "add",
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
    ] in runner.commands()
    add_call = next(
        call for call in runner.calls if has_git_subcommand(call[0], "add")
    )
    assert add_call[2] == "".join(
        f":(literal){path}\0" for path in fake_prepared_paths(tmp_path)
    )
    assert [
        "git",
        "commit",
        "-m",
        "data: publish dashboard 2026-08-11T03:04:05+00:00",
    ] in runner.commands()
    assert ["git", "push", "origin", "gh-pages"] in runner.commands()
    assert git_subcommand_index(runner, "add") < command_index(
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
    assert command_index(runner, ["git", "worktree", "add"]) < command_index(
        runner, ["git", "ls-remote"]
    )


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
        "+refs/heads/gh-pages:refs/remotes/origin/gh-pages",
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

    with pytest.raises(
        RuntimeError,
        match=rf"Git command ls-remote failed with exit code {remote_code}",
    ):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not worktree.exists()
    assert not any(command[:2] == ["git", "mktree"] for command in runner.commands())


@pytest.mark.parametrize(
    ("top_level", "branch", "status", "message"),
    [
        ("other", "gh-pages", "", "top-level"),
        (None, "main", "", "branch"),
        (None, "gh-pages", " M index.html\0", "clean"),
        (None, "gh-pages", "?? notes.txt\0", "clean"),
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

    assert not any(has_git_subcommand(command, "add") for command in runner.commands())


def test_prestaged_index_change_aborts_before_copy_or_add(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    sentinel = worktree / "index.html"
    sentinel.write_text("user index content", encoding="utf-8")
    runner = FakeRunner(tmp_path, worktree, status="M  index.html\0")

    with pytest.raises(RuntimeError, match="clean"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert sentinel.read_text(encoding="utf-8") == "user index content"
    assert [
        "git",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ] in runner.commands()
    assert not any(has_git_subcommand(command, "add") for command in runner.commands())


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
        (".nojekyll", b"\xff\xfe", "UTF-8"),
        (".nojekyll", b"API_KEY=secret\n", "unsafe public output"),
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


def test_nojekyll_marker_must_be_empty_before_git_or_writes(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    (site / ".nojekyll").write_text("safe but unexpected\n", encoding="utf-8")
    worktree = prepare_existing_worktree(tmp_path)
    sentinel = worktree / "index.html"
    sentinel.write_text("old", encoding="utf-8")
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="empty"):
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
    assert not any(has_git_subcommand(command, "add") for command in runner.commands())


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

    def replace_then_swap(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> None:
        nonlocal swapped
        original_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]
        if not swapped and Path(destination).name == ".nojekyll":
            worktree.rename(parked)
            try:
                os.symlink(external, worktree, target_is_directory=True)
            except OSError as error:
                parked.rename(worktree)
                pytest.skip(f"symlinks unavailable: {error}")
            swapped = True

    monkeypatch.setattr(module.os, "replace", replace_then_swap)
    try:
        with pytest.raises((OSError, ValueError)):
            publisher(tmp_path, worktree, runner).publish(site)
        assert list(external.iterdir()) == []
    finally:
        if swapped:
            worktree.unlink()
            parked.rename(worktree)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction race regression")
def test_windows_parent_junction_swap_cannot_open_external_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.dashboard_publish as module

    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    external = tmp_path / "external-assets"
    external.mkdir()
    sentinel = external / "app.js"
    sentinel.write_bytes(b"external sentinel")
    parked = tmp_path / "parked-assets"
    runner = FakeRunner(tmp_path, worktree)
    original_open = module.os.open
    swapped = False
    external_temp_open_attempted = False

    def swap_before_temp_open(
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped, external_temp_open_attempted
        candidate = Path(path) if not isinstance(path, int) else None
        assets = worktree / "assets"
        if (
            not swapped
            and candidate is not None
            and candidate.parent == assets
            and candidate.name.startswith(".xrag-publish-")
            and candidate.name.endswith(".tmp")
        ):
            assets.rename(parked)
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(assets), str(external)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                parked.rename(assets)
                pytest.skip(f"junctions unavailable: {result.stderr or result.stdout}")
            swapped = True
            external_temp_open_attempted = True
        if dir_fd is None:
            return original_open(path, flags, mode)  # type: ignore[arg-type]
        return original_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]

    monkeypatch.setattr(module.os, "open", swap_before_temp_open)
    try:
        with pytest.raises((OSError, ValueError)):
            publisher(tmp_path, worktree, runner).publish(site)
        assert not external_temp_open_attempted
        assert sentinel.read_bytes() == b"external sentinel"
    finally:
        if swapped:
            (worktree / "assets").rmdir()
            parked.rename(worktree / "assets")


def test_startup_preserves_unproven_publisher_temp_lookalikes(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    assets = worktree / "assets"
    assets.mkdir()
    owned = assets / f".xrag-publish-{'a' * 32}.tmp"
    lookalike = assets / ".xrag-publish-not-owned.tmp"
    owned.write_bytes(b"incomplete publisher write")
    lookalike.write_bytes(b"user file")
    runner = FakeRunner(tmp_path, worktree, diff_code=0)

    publisher(tmp_path, worktree, runner).publish(site)

    assert owned.read_bytes() == b"incomplete publisher write"
    assert lookalike.read_bytes() == b"user file"


def test_real_unrelated_temp_lookalike_is_never_removed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    initialize_project_repository(root)
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    initialize_linked_pages_worktree(root, worktree)
    unrelated = worktree / "unrelated" / f".xrag-publish-{'c' * 32}.tmp"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"user-owned sentinel")

    with pytest.raises(RuntimeError, match="safely resumable"):
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert unrelated.read_bytes() == b"user-owned sentinel"


def test_unproven_publisher_temp_symlink_is_preserved_without_touching_target(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    external = tmp_path / "external-temp-content"
    external.write_bytes(b"sentinel")
    linked_temp = worktree / f".xrag-publish-{'b' * 32}.tmp"
    try:
        os.symlink(external, linked_temp)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    runner = FakeRunner(tmp_path, worktree)

    publisher(tmp_path, worktree, runner).publish(site)

    assert external.read_bytes() == b"sentinel"
    assert linked_temp.is_symlink()


def test_source_substitution_after_preparation_does_not_change_copied_bytes(
    tmp_path: Path,
) -> None:
    site = prepare_site(tmp_path)
    source = site / "assets" / "app.js"
    expected = source.read_bytes()
    worktree = prepare_existing_worktree(tmp_path)
    delegate = FakeRunner(tmp_path, worktree, diff_code=0)
    substituted = False

    def substitute_after_preparation(
        command: list[str], cwd: Path, input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        nonlocal substituted
        if not substituted:
            source.write_bytes(b"const replacement = true;\n")
            substituted = True
        return delegate(command, cwd, input_text)

    publisher(tmp_path, worktree, substitute_after_preparation).publish(site)

    assert substituted
    assert (worktree / "assets" / "app.js").read_bytes() == expected


def test_source_replacement_between_walk_and_open_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.dashboard_publish as module

    site = prepare_site(tmp_path)
    source = site / "assets" / "app.js"
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)
    original_walk = module._walk_real_tree

    def walk_then_replace(root: Path, label: str) -> object:
        discovered = original_walk(root, label)
        replacement = source.with_name("replacement.js")
        replacement.write_text("const replacement = true;\n", encoding="utf-8")
        os.replace(replacement, source)
        return discovered

    monkeypatch.setattr(module, "_walk_real_tree", walk_then_replace)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []


def test_source_hard_link_is_rejected_before_git(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    source = site / "assets" / "app.js"
    source.unlink()
    external = tmp_path / "external-app.js"
    external.write_text("const external = true;\n", encoding="utf-8")
    try:
        os.link(external, source)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree)

    with pytest.raises(ValueError, match="source"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert runner.calls == []
    assert external.read_text(encoding="utf-8") == "const external = true;\n"


@pytest.mark.parametrize(
    "unexpected",
    [
        "private.txt",
        "assets/source.map",
        "assets/evil.js",
        "assets\\escaped.js",
        "data/evil.json",
        "data/../private.json",
    ],
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
    ("failing_command", "label", "push_expected"),
    [
        (
            (
                "git",
                "commit",
                "-m",
                "data: publish dashboard 2026-08-11T03:04:05+00:00",
            ),
            "commit",
            False,
        ),
        (("git", "push", "origin", "gh-pages"), "push", True),
    ],
)
def test_commit_and_push_failures_do_not_claim_success_or_dump_multiline_errors(
    tmp_path: Path,
    failing_command: tuple[str, ...],
    label: str,
    push_expected: bool,
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(
        tmp_path,
        worktree,
        failures={
            failing_command: (
                17,
                "",
                "https://user:secret-token@example.invalid/private failed\n"
                "secret second line\n",
            )
        },
    )

    with pytest.raises(RuntimeError) as caught:
        publisher(tmp_path, worktree, runner).publish(site)

    assert str(caught.value) == f"Git command {label} failed with exit code 17"
    assert "secret" not in str(caught.value)
    assert "example.invalid" not in str(caught.value)
    assert (
        any(command[:2] == ["git", "push"] for command in runner.commands())
        is push_expected
    )


def test_unexpected_cached_diff_exit_code_raises(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree, diff_code=2)

    with pytest.raises(RuntimeError, match="Git command diff failed with exit code 2"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())


def test_staged_blob_hash_must_match_prepared_bytes(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    runner = FakeRunner(tmp_path, worktree, index_oid_override="f" * 40)

    with pytest.raises(RuntimeError, match="staged content"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())


@pytest.mark.parametrize("identity", ["GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"])
def test_missing_identity_aborts_before_normal_commit(
    tmp_path: Path, identity: str
) -> None:
    site = prepare_site(tmp_path)
    worktree = prepare_existing_worktree(tmp_path)
    failing = ("git", "var", identity)
    runner = FakeRunner(
        tmp_path,
        worktree,
        failures={
            failing: (
                128,
                "",
                "https://user:identity-secret@example.invalid/missing\n",
            )
        },
    )

    with pytest.raises(RuntimeError) as caught:
        publisher(tmp_path, worktree, runner).publish(site)

    assert str(caught.value) == "Git command var failed with exit code 128"
    assert "secret" not in str(caught.value)
    assert not worktree.joinpath("index.html").exists()
    assert not any(has_git_subcommand(command, "add") for command in runner.commands())
    assert not any(command[:2] == ["git", "commit"] for command in runner.commands())


def test_missing_identity_aborts_before_empty_branch_plumbing(tmp_path: Path) -> None:
    site = prepare_site(tmp_path)
    worktree = tmp_path / ".worktrees" / "x-rag-pages"
    failing = ("git", "var", "GIT_AUTHOR_IDENT")
    runner = FakeRunner(
        tmp_path,
        worktree,
        local_branch=False,
        remote_code=2,
        failures={failing: (128, "", "secret identity output\n")},
    )

    with pytest.raises(RuntimeError, match="Git command var failed with exit code 128"):
        publisher(tmp_path, worktree, runner).publish(site)

    assert not any(command[:2] == ["git", "mktree"] for command in runner.commands())


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


def test_default_runner_accepts_utf8_bytes_input(tmp_path: Path) -> None:
    import xrag.dashboard_publish as module

    real_git(tmp_path, "init")

    result = module._default_runner(
        ["git", "hash-object", "--stdin"], tmp_path, b"payload"
    )

    assert result.returncode == 0
    assert isinstance(result.stdout, str)


def real_git(
    cwd: Path, *arguments: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"test git command failed: {arguments[0]} ({result.returncode})"
        )
    return result


def initialize_project_repository(root: Path) -> None:
    root.mkdir()
    real_git(root, "init", "-b", "main")
    real_git(root, "config", "user.name", "Publisher Test")
    real_git(root, "config", "user.email", "publisher@example.invalid")
    root.joinpath("README.md").write_text("test repository\n", encoding="utf-8")
    real_git(root, "add", "--", "README.md")
    real_git(root, "commit", "-m", "initial")


def initialize_linked_pages_worktree(root: Path, worktree: Path) -> None:
    empty_tree = real_git(root, "mktree", input_text="").stdout.strip()
    commit = real_git(
        root,
        "commit-tree",
        empty_tree,
        "-m",
        "initialize pages",
    ).stdout.strip()
    real_git(root, "branch", "gh-pages", commit)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    real_git(root, "worktree", "add", str(worktree), "gh-pages")


def test_real_am_user_staged_blob_is_not_claimed_as_recoverable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    initialize_project_repository(root)
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    initialize_linked_pages_worktree(root, worktree)
    target = worktree / "index.html"
    target.write_text("user staged content\n", encoding="utf-8")
    real_git(worktree, "add", "--", "index.html")
    staged_before = real_git(worktree, "show", ":index.html").stdout
    target.write_bytes((site / "index.html").read_bytes())
    assert real_git(worktree, "status", "--porcelain=v1").stdout.startswith(
        "AM index.html"
    )

    with pytest.raises(RuntimeError, match="safely resumable"):
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert real_git(worktree, "show", ":index.html").stdout == staged_before
    assert real_git(worktree, "status", "--porcelain=v1").stdout.startswith(
        "AM index.html"
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows argv regression")
def test_native_windows_many_long_paths_use_stdin_pathspecs(tmp_path: Path) -> None:
    import xrag.dashboard_publish as module

    root = tmp_path / "project"
    initialize_project_repository(root)
    site = prepare_site(root)
    for index in range(600):
        path = site / "assets" / f"chunk-{index:04d}-{'x' * 50}.js"
        path.write_text(f"window.chunk{index} = true;\n", encoding="utf-8")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    real_git(remote, "init", "--bare")
    real_git(root, "remote", "add", "origin", str(remote))
    worktree = root / ".worktrees" / "x-rag-pages"
    initialize_linked_pages_worktree(root, worktree)
    calls: list[tuple[list[str], str | bytes | None]] = []

    def recording_runner(
        command: list[str], cwd: Path, input_text: str | bytes | None
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), input_text))
        return module._default_runner(command, cwd, input_text)

    result = PagesPublisher(
        root, worktree, runner=recording_runner, clock=lambda: NOW
    ).publish(site)

    assert result == {"changed": True, "branch": "gh-pages"}
    add_calls = [
        (command, stdin)
        for command, stdin in calls
        if has_git_subcommand(command, "add")
    ]
    assert len(add_calls) == 1
    command, stdin = add_calls[0]
    assert "--pathspec-from-file=-" in command
    assert "--pathspec-file-nul" in command
    assert isinstance(stdin, (str, bytes)) and len(stdin) > 32_767
    assert len(subprocess.list2cmdline(command)) < 32_767


def test_real_independent_gh_pages_repository_is_rejected_before_copy(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    initialize_project_repository(root)
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    worktree.mkdir(parents=True)
    real_git(worktree, "init", "-b", "gh-pages")
    real_git(worktree, "config", "user.name", "Publisher Test")
    real_git(worktree, "config", "user.email", "publisher@example.invalid")
    sentinel = worktree / "index.html"
    sentinel.write_text("independent repository\n", encoding="utf-8")
    real_git(worktree, "add", "--", "index.html")
    real_git(worktree, "commit", "-m", "independent")

    with pytest.raises(RuntimeError, match="linked worktree"):
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert sentinel.read_text(encoding="utf-8") == "independent repository\n"
    assert real_git(worktree, "rev-parse", "HEAD").stdout.strip() == real_git(
        worktree, "rev-parse", "gh-pages"
    ).stdout.strip()


def test_real_project_root_must_be_repository_top_level(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    initialize_project_repository(repository)
    root = repository / "nested-project"
    root.mkdir()
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"

    with pytest.raises(RuntimeError, match="project root"):
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert not worktree.exists()


def test_real_failed_push_is_retried_when_local_head_is_unpublished(
    tmp_path: Path,
) -> None:
    import xrag.dashboard_publish as module

    root = tmp_path / "project"
    initialize_project_repository(root)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    real_git(remote, "init", "--bare")
    real_git(root, "remote", "add", "origin", str(remote))
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    failed_pushes = 0

    def fail_first_push(
        command: list[str], cwd: Path, input_text: str | None
    ) -> subprocess.CompletedProcess[str]:
        nonlocal failed_pushes
        if command[:2] == ["git", "push"] and failed_pushes == 0:
            failed_pushes += 1
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "https://user:secret-token@example.invalid/private failed\n",
            )
        return module._default_runner(command, cwd, input_text)

    with pytest.raises(RuntimeError):
        PagesPublisher(
            root, worktree, runner=fail_first_push, clock=lambda: NOW
        ).publish(site)
    local_head = real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    absent_remote = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/gh-pages"],
        cwd=remote,
        check=False,
    )
    assert absent_remote.returncode == 1

    result = PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert result == {"changed": True, "branch": "gh-pages"}
    assert real_git(remote, "rev-parse", "refs/heads/gh-pages").stdout.strip() == (
        local_head
    )


def test_real_commit_hook_failure_can_resume_without_overwriting_other_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    initialize_project_repository(root)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    real_git(remote, "init", "--bare")
    real_git(root, "remote", "add", "origin", str(remote))
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    initialize_linked_pages_worktree(root, worktree)
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\necho 'https://user:secret@example.invalid/hook' >&2\nexit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    with pytest.raises(RuntimeError) as caught:
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)
    assert "secret" not in str(caught.value)
    assert real_git(worktree, "diff", "--cached", "--name-only").stdout
    hook.unlink()

    result = PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert result == {"changed": True, "branch": "gh-pages"}
    assert real_git(worktree, "status", "--porcelain=v1").stdout == ""
    assert real_git(remote, "rev-parse", "refs/heads/gh-pages").stdout.strip() == (
        real_git(worktree, "rev-parse", "HEAD").stdout.strip()
    )


def test_real_partial_copy_failure_can_resume_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.dashboard_publish as module

    root = tmp_path / "project"
    initialize_project_repository(root)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    real_git(remote, "init", "--bare")
    real_git(root, "remote", "add", "origin", str(remote))
    site = prepare_site(root)
    worktree = root / ".worktrees" / "x-rag-pages"
    initialize_linked_pages_worktree(root, worktree)
    original_replace = module.os.replace
    failed = False

    def fail_one_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> None:
        nonlocal failed
        if not failed and Path(destination).name == "app.js":
            failed = True
            raise OSError("injected copy failure")
        original_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(module.os, "replace", fail_one_copy)
    with pytest.raises(OSError, match="injected copy failure"):
        PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)
    assert failed
    assert worktree.joinpath(".nojekyll").is_file()

    result = PagesPublisher(root, worktree, clock=lambda: NOW).publish(site)

    assert result == {"changed": True, "branch": "gh-pages"}
    assert real_git(worktree, "status", "--porcelain=v1").stdout == ""
