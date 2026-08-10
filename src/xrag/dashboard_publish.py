from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Protocol

from .dashboard_export import assert_public_content


_TEXT_SUFFIXES = {".html", ".css", ".js", ".json"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_STAGE_PATHS = [".nojekyll", "index.html", "assets", "data"]


class Runner(Protocol):
    def __call__(
        self, command: list[str], cwd: Path, input_text: str | None
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    command: list[str], cwd: Path, input_text: str | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


class PagesPublisher:
    def __init__(
        self,
        root: Path,
        worktree: Path,
        *,
        runner: Runner = _default_runner,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).absolute()
        self.worktree = Path(worktree).absolute()
        self._runner = runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def publish(self, site_dir: Path) -> dict[str, object]:
        prepared = self._prepare_source(Path(site_dir))
        self._validate_worktree_location()
        if _path_exists(self.worktree):
            self._validate_existing_worktree()
        else:
            self._create_worktree()
            self._validate_existing_worktree()

        guard = _DestinationGuard(self.root, self.worktree)
        for relative, content in prepared:
            _write_atomic(self.worktree / relative, content, guard)

        self._git_checked(
            ["git", "add", "--", *_STAGE_PATHS],
            cwd=self.worktree,
        )
        staged = self._git_checked(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=self.worktree,
        )
        self._validate_staged_paths(staged.stdout)
        changed = self._git(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.worktree,
        )
        if changed.returncode == 0:
            return {"changed": False, "branch": "gh-pages"}
        if changed.returncode != 1:
            raise _git_error(["git", "diff"], changed)

        timestamp = self._clock().isoformat(timespec="seconds")
        self._git_checked(
            ["git", "commit", "-m", f"data: publish dashboard {timestamp}"],
            cwd=self.worktree,
        )
        self._git_checked(
            ["git", "push", "origin", "gh-pages"],
            cwd=self.worktree,
        )
        return {"changed": True, "branch": "gh-pages"}

    def _prepare_source(self, site_dir: Path) -> list[tuple[Path, bytes]]:
        expected = self.root / "data" / "dashboard-site"
        if site_dir.absolute() != expected:
            raise ValueError("unsafe dashboard source")
        _validate_real_root(self.root, "dashboard source")
        _validate_chain(self.root, expected, "dashboard source", require_final=True)
        if not expected.is_dir():
            raise ValueError("dashboard source is incomplete")

        discovered = _walk_real_tree(expected, "dashboard source")
        required = (Path("index.html"), Path(".nojekyll"), Path("data/latest.json"))
        discovered_paths = {relative for relative, _ in discovered}
        if any(relative not in discovered_paths for relative in required):
            raise ValueError("dashboard source is incomplete")

        prepared: list[tuple[Path, bytes]] = []
        for relative, source in discovered:
            if not _is_allowed(relative):
                continue
            try:
                content = source.read_bytes()
            except OSError as error:
                raise ValueError("dashboard source is unreadable") from error
            suffix = relative.suffix.lower()
            if suffix in _TEXT_SUFFIXES or relative == Path(".nojekyll"):
                try:
                    decoded = content.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ValueError(
                        "dashboard source contains malformed UTF-8"
                    ) from error
                assert_public_content(decoded)
                if relative == Path(".nojekyll") and decoded:
                    raise ValueError("dashboard .nojekyll marker must be empty")
                if suffix == ".json":
                    try:
                        json.loads(decoded)
                    except json.JSONDecodeError as error:
                        raise ValueError(
                            "dashboard source contains malformed JSON"
                        ) from error
            elif suffix in _IMAGE_SUFFIXES and not _valid_image(content, suffix):
                raise ValueError("dashboard source contains an invalid image")
            prepared.append((relative, content))
        return sorted(prepared, key=lambda item: item[0].as_posix())

    def _validate_worktree_location(self) -> None:
        _validate_real_root(self.root, "dashboard worktree")
        try:
            relative = self.worktree.relative_to(self.root)
        except ValueError as error:
            raise ValueError("unsafe dashboard worktree") from error
        if not relative.parts:
            raise ValueError("unsafe dashboard worktree")
        _validate_chain(
            self.root,
            self.worktree,
            "dashboard worktree",
            require_final=False,
        )

    def _create_worktree(self) -> None:
        _ensure_real_directories(self.root, self.worktree.parent, "dashboard worktree")
        local = self._git(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/gh-pages"],
            cwd=self.root,
        )
        if local.returncode == 1:
            remote = self._git(
                [
                    "git",
                    "ls-remote",
                    "--exit-code",
                    "--heads",
                    "origin",
                    "refs/heads/gh-pages",
                ],
                cwd=self.root,
            )
            if remote.returncode == 0:
                self._git_checked(
                    [
                        "git",
                        "fetch",
                        "origin",
                        "refs/heads/gh-pages:refs/remotes/origin/gh-pages",
                    ],
                    cwd=self.root,
                )
                self._git_checked(
                    ["git", "branch", "--track", "gh-pages", "origin/gh-pages"],
                    cwd=self.root,
                )
            elif remote.returncode == 2:
                self._initialize_empty_branch()
            else:
                raise _git_error(["git", "ls-remote"], remote)
        elif local.returncode != 0:
            raise _git_error(["git", "show-ref"], local)

        self._git_checked(
            ["git", "worktree", "add", str(self.worktree), "gh-pages"],
            cwd=self.root,
        )

    def _initialize_empty_branch(self) -> None:
        tree_result = self._git_checked(["git", "mktree"], cwd=self.root, input_text="")
        tree = _single_output_token(tree_result, "git mktree")
        commit_result = self._git_checked(
            [
                "git",
                "commit-tree",
                tree,
                "-m",
                "chore: initialize dashboard pages",
            ],
            cwd=self.root,
        )
        commit = _single_output_token(commit_result, "git commit-tree")
        self._git_checked(
            ["git", "branch", "gh-pages", commit],
            cwd=self.root,
        )

    def _validate_existing_worktree(self) -> None:
        _validate_chain(
            self.root,
            self.worktree,
            "dashboard worktree",
            require_final=True,
        )
        if not self.worktree.is_dir():
            raise ValueError("unsafe dashboard worktree")
        git_marker = self.worktree / ".git"
        if (
            not _path_exists(git_marker)
            or _is_link_or_reparse(git_marker)
            or not (git_marker.is_file() or git_marker.is_dir())
        ):
            raise RuntimeError("destination is not a real Git worktree")

        top = self._git_checked(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.worktree
        ).stdout.strip()
        if not top:
            raise RuntimeError("Git worktree top-level is unavailable")
        try:
            actual_top = Path(top).absolute()
        except OSError as error:
            raise RuntimeError("Git worktree top-level is invalid") from error
        if actual_top != self.worktree:
            raise RuntimeError("Git worktree top-level does not match destination")

        status = self._git_checked(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            cwd=self.worktree,
        )
        if status.stdout:
            raise RuntimeError("Git worktree must be clean before publishing")
        branch = self._git_checked(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=self.worktree,
        ).stdout.strip()
        if branch != "gh-pages":
            raise RuntimeError("Git worktree branch must be gh-pages")

    def _validate_staged_paths(self, output: str) -> None:
        if not output:
            return
        if not output.endswith("\0"):
            raise RuntimeError("unexpected staged path")
        for raw_path in output[:-1].split("\0"):
            if (
                "\\" in raw_path
                or not raw_path
                or not _is_allowed_staged(Path(raw_path))
            ):
                raise RuntimeError("unexpected staged path")

    def _git(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(command, cwd, input_text)

    def _git_checked(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._git(command, cwd=cwd, input_text=input_text)
        if result.returncode != 0:
            raise _git_error(command, result)
        return result


def _path_exists(path: Path) -> bool:
    return path.exists() or _is_link_or_reparse(path)


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    try:
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _validate_real_root(root: Path, label: str) -> None:
    if _is_link_or_reparse(root) or not root.is_dir():
        raise ValueError(f"unsafe {label}")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"unsafe {label}") from error
    if resolved != root:
        raise ValueError(f"unsafe {label}")


def _validate_chain(
    root: Path,
    target: Path,
    label: str,
    *,
    require_final: bool,
) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"unsafe {label}") from error
    current = root
    for part in relative.parts:
        current = current / part
        exists = _path_exists(current)
        if _is_link_or_reparse(current):
            raise ValueError(f"unsafe {label}")
        if exists:
            try:
                resolved = current.resolve(strict=True)
            except OSError as error:
                raise ValueError(f"unsafe {label}") from error
            if resolved != current:
                raise ValueError(f"unsafe {label}")
        elif require_final:
            raise ValueError(f"unsafe {label}")


def _walk_real_tree(root: Path, label: str) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise ValueError(f"unsafe {label}") from error
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink() or _is_link_or_reparse(path):
                raise ValueError(f"unsafe {label}")
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError as error:
                raise ValueError(f"unsafe {label}") from error
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                files.append((path.relative_to(root), path))
            else:
                raise ValueError(f"unsafe {label}")
    return files


def _is_allowed(relative: Path) -> bool:
    if relative == Path("index.html") or relative == Path(".nojekyll"):
        return True
    if len(relative.parts) < 2:
        return False
    suffix = relative.suffix.lower()
    if relative.parts[0] == "assets":
        return suffix in {".css", ".js", *_IMAGE_SUFFIXES}
    if relative.parts[0] == "data":
        return suffix == ".json"
    return False


def _is_allowed_staged(path: Path) -> bool:
    if path.is_absolute() or ".." in path.parts:
        return False
    return _is_allowed(path)


def _valid_image(content: bytes, suffix: str) -> bool:
    if suffix in {".jpg", ".jpeg"}:
        return content.startswith(b"\xff\xd8\xff")
    if suffix == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
    return False


def _ensure_real_directories(root: Path, directory: Path, label: str) -> None:
    try:
        relative = directory.relative_to(root)
    except ValueError as error:
        raise ValueError(f"unsafe {label}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise ValueError(f"unsafe {label}")
        if current.exists():
            if not current.is_dir() or current.resolve() != current:
                raise ValueError(f"unsafe {label}")
        else:
            try:
                current.mkdir()
            except OSError as error:
                raise ValueError(f"unsafe {label}") from error
        if _is_link_or_reparse(current) or current.resolve() != current:
            raise ValueError(f"unsafe {label}")


class _DestinationGuard:
    def __init__(self, root: Path, worktree: Path) -> None:
        self.root = root
        self.worktree = worktree
        _validate_real_root(root, "dashboard target")
        _validate_chain(root, worktree, "dashboard target", require_final=True)

    def validate_target(self, target: Path) -> None:
        target = target.absolute()
        _validate_real_root(self.root, "dashboard target")
        _validate_chain(
            self.root, self.worktree, "dashboard target", require_final=True
        )
        try:
            relative = target.relative_to(self.worktree)
        except ValueError as error:
            raise ValueError("unsafe dashboard target") from error
        if not relative.parts:
            raise ValueError("unsafe dashboard target")
        _ensure_real_directories(
            self.worktree, target.parent, "dashboard target"
        )
        if _is_link_or_reparse(target) or (target.exists() and not target.is_file()):
            raise ValueError("unsafe dashboard target")
        expected_parent = self.worktree / relative.parent
        if target.parent.resolve(strict=True) != expected_parent:
            raise ValueError("unsafe dashboard target")


def _write_atomic(path: Path, content: bytes, guard: _DestinationGuard) -> None:
    guard.validate_target(path)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".xrag-publish-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        guard.validate_target(path)
        os.replace(temporary_path, path)
        temporary_path = None
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _git_error(
    command: list[str], result: subprocess.CompletedProcess[str]
) -> RuntimeError:
    label = " ".join(command[:2])
    detail = _first_line(result.stderr) or _first_line(result.stdout)
    message = f"{label} failed"
    if detail:
        message = f"{message}: {detail}"
    return RuntimeError(message)


def _first_line(value: str | None) -> str:
    if not value:
        return ""
    lines = value.splitlines()
    return lines[0] if lines else ""


def _single_output_token(
    result: subprocess.CompletedProcess[str], label: str
) -> str:
    token = _first_line(result.stdout).strip()
    if not token or any(character.isspace() for character in token):
        raise RuntimeError(f"{label} returned invalid output")
    return token
