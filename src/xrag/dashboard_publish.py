from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Protocol
import uuid

from .dashboard_export import assert_public_content


_TEXT_SUFFIXES = {".html", ".css", ".js", ".json"}
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
_TEMP_NAME = re.compile(r"\.xrag-publish-[0-9a-f]{32}\.tmp\Z")


@dataclass(frozen=True)
class _PreparedFile:
    relative: Path
    content: bytes
    digest: str


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
        common_git_dir = self._validate_project_repository()
        if _path_exists(self.worktree):
            self._validate_existing_worktree(common_git_dir)
        else:
            self._create_worktree()
            self._validate_existing_worktree(common_git_dir)

        guard = _DestinationGuard(self.root, self.worktree)
        _clean_publisher_temps(self.worktree, guard)
        self._validate_recoverable_state(prepared)
        for item in prepared:
            _write_atomic(self.worktree / item.relative, item.content, guard)

        prepared_paths = [item.relative.as_posix() for item in prepared]
        self._git_checked(
            [
                "git",
                "-c",
                "core.autocrlf=false",
                "-c",
                "core.safecrlf=false",
                "add",
                "--",
                *map(_literal_pathspec, prepared_paths),
            ],
            cwd=self.worktree,
        )
        self._validate_staged_content(prepared)
        changed = self._git(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.worktree,
        )
        if changed.returncode == 0:
            pushed = self._push_if_unpublished()
            return {"changed": pushed, "branch": "gh-pages"}
        if changed.returncode != 1:
            raise _git_error(["git", "diff"], changed)

        self._preflight_identity(self.worktree)
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

    def _prepare_source(self, site_dir: Path) -> list[_PreparedFile]:
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

        prepared: list[_PreparedFile] = []
        for relative, source in discovered:
            if not _is_allowed(relative):
                continue
            try:
                content = _read_source_bytes(source)
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
            prepared.append(
                _PreparedFile(relative, content, hashlib.sha256(content).hexdigest())
            )
        return sorted(prepared, key=lambda item: item.relative.as_posix())

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
                        "+refs/heads/gh-pages:refs/remotes/origin/gh-pages",
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

    def _validate_project_repository(self) -> Path:
        top = self._git_checked(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.root
        )
        if _normalized_git_path(top.stdout, self.root) != self.root:
            raise RuntimeError("project root must be the Git top-level")
        common = self._git_checked(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.root
        )
        return _normalized_git_path(common.stdout, self.root)

    def _initialize_empty_branch(self) -> None:
        self._preflight_identity(self.root)
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

    def _preflight_identity(self, cwd: Path) -> None:
        self._git_checked(["git", "var", "GIT_AUTHOR_IDENT"], cwd=cwd)
        self._git_checked(["git", "var", "GIT_COMMITTER_IDENT"], cwd=cwd)

    def _validate_existing_worktree(self, project_common_git_dir: Path) -> None:
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
            or not git_marker.is_file()
        ):
            raise RuntimeError("destination is not a linked worktree")

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
        destination_common = self._git_checked(
            ["git", "rev-parse", "--git-common-dir"], cwd=self.worktree
        )
        if (
            _normalized_git_path(destination_common.stdout, self.worktree)
            != project_common_git_dir
        ):
            raise RuntimeError("destination is not a linked worktree of project root")
        listed = self._git_checked(
            ["git", "worktree", "list", "--porcelain", "-z"], cwd=self.root
        )
        if self.worktree not in _listed_worktrees(listed.stdout):
            raise RuntimeError("destination is not a registered linked worktree")

        branch = self._git_checked(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=self.worktree,
        ).stdout.strip()
        if branch != "gh-pages":
            raise RuntimeError("Git worktree branch must be gh-pages")

    def _validate_recoverable_state(
        self, prepared: list[_PreparedFile]
    ) -> None:
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
        prepared_by_path = {
            item.relative.as_posix(): item for item in prepared
        }
        for relative in _status_paths(status.stdout):
            item = prepared_by_path.get(relative.as_posix())
            if item is None or not _destination_has_bytes(
                self.worktree, relative, item.content
            ):
                raise RuntimeError(
                    "Git worktree must be clean or safely resumable"
                )

    def _validate_staged_content(self, prepared: list[_PreparedFile]) -> None:
        prepared_by_path = {
            item.relative.as_posix(): item for item in prepared
        }
        arguments = list(prepared_by_path)
        staged_result = self._git_checked(
            ["git", "diff", "--cached", "--name-only", "-z"],
            cwd=self.worktree,
        )
        try:
            staged = {
                path.as_posix() for path in _nul_paths(staged_result.stdout)
            }
        except RuntimeError as error:
            raise RuntimeError("unexpected staged path") from error
        if not staged.issubset(prepared_by_path):
            raise RuntimeError("unexpected staged path")

        object_format_result = self._git_checked(
            ["git", "rev-parse", "--show-object-format"], cwd=self.worktree
        )
        object_format = _first_line(object_format_result.stdout).strip()
        if object_format not in {"sha1", "sha256"}:
            raise RuntimeError("Git command rev-parse returned invalid output")
        expected_oids = {
            path: _git_blob_oid(item.content, object_format)
            for path, item in prepared_by_path.items()
        }
        index_result = self._git_checked(
            [
                "git",
                "ls-files",
                "--stage",
                "-z",
                "--",
                *map(_literal_pathspec, arguments),
            ],
            cwd=self.worktree,
        )
        index_oids = _index_oids(index_result.stdout)
        if index_oids != expected_oids:
            raise RuntimeError("staged content does not match prepared dashboard")
        head_result = self._git_checked(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "HEAD",
                "--",
                *map(_literal_pathspec, arguments),
            ],
            cwd=self.worktree,
        )
        head_oids = _tree_oids(head_result.stdout)
        expected_changed = {
            path
            for path, expected_oid in expected_oids.items()
            if head_oids.get(path) != expected_oid
        }
        if staged != expected_changed:
            raise RuntimeError("unexpected staged path")

    def _push_if_unpublished(self) -> bool:
        local = self._git_checked(["git", "rev-parse", "HEAD"], cwd=self.worktree)
        local_head = _object_id(local.stdout, "rev-parse")
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
            first = _first_line(remote.stdout)
            fields = first.split("\t", 1)
            if len(fields) != 2 or fields[1] != "refs/heads/gh-pages":
                raise RuntimeError("Git command ls-remote returned invalid output")
            if _object_id(fields[0], "ls-remote") == local_head:
                return False
        elif remote.returncode != 2:
            raise _git_error(["git", "ls-remote"], remote)
        self._git_checked(
            ["git", "push", "origin", "gh-pages"], cwd=self.worktree
        )
        return True

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
                source_stat = path.lstat()
                mode = source_stat.st_mode
            except OSError as error:
                raise ValueError(f"unsafe {label}") from error
            if stat.S_ISDIR(mode):
                pending.append(path)
            elif stat.S_ISREG(mode):
                if source_stat.st_nlink != 1:
                    raise ValueError(f"unsafe {label}")
                files.append((path.relative_to(root), path))
            else:
                raise ValueError(f"unsafe {label}")
    return files


def _read_source_bytes(path: Path) -> bytes:
    if _is_link_or_reparse(path):
        raise OSError("unsafe source file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        source_stat = os.fstat(descriptor)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise OSError("unsafe source file")
        with os.fdopen(descriptor, "rb") as source_file:
            descriptor = -1
            return source_file.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def _clean_publisher_temps(
    worktree: Path, guard: _DestinationGuard
) -> None:
    pending = [worktree]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            raise ValueError("unsafe dashboard target") from error
        for entry in entries:
            if directory == worktree and entry.name == ".git":
                continue
            path = Path(entry.path)
            matches_temp = bool(_TEMP_NAME.fullmatch(entry.name))
            if matches_temp:
                _validate_chain(
                    worktree,
                    path.parent,
                    "dashboard target",
                    require_final=True,
                )
                if _is_link_or_reparse(path):
                    raise ValueError("unsafe publisher temporary file")
                try:
                    mode = entry.stat(follow_symlinks=False).st_mode
                except OSError as error:
                    raise ValueError("unsafe publisher temporary file") from error
                if not stat.S_ISREG(mode):
                    raise ValueError("unsafe publisher temporary file")
                guard.validate_target(path)
                try:
                    path.unlink()
                except OSError as error:
                    raise ValueError("unsafe publisher temporary file") from error
                continue
            if entry.is_symlink() or _is_link_or_reparse(path):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
            except OSError as error:
                raise ValueError("unsafe dashboard target") from error


def _write_atomic(path: Path, content: bytes, guard: _DestinationGuard) -> None:
    guard.validate_target(path)
    temporary_path: Path | None = (
        path.parent / f".xrag-publish-{uuid.uuid4().hex}.tmp"
    )
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        binary_flag = getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary_path, flags | binary_flag, 0o600)
        with os.fdopen(descriptor, "wb") as temporary:
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
    label = _git_subcommand(command)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", label):
        label = "unknown"
    return RuntimeError(
        f"Git command {label} failed with exit code {result.returncode}"
    )


def _git_subcommand(command: list[str]) -> str:
    index = 1
    while index < len(command) and command[index] == "-c":
        index += 2
    return command[index] if index < len(command) else "unknown"


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


def _status_paths(output: str) -> list[Path]:
    paths: list[Path] = []
    for record in _nul_records(output):
        if len(record) < 4 or record[2] != " ":
            raise RuntimeError("Git worktree status returned invalid output")
        if record[0] in {"R", "C"} or record[1] in {"R", "C"}:
            raise RuntimeError("Git worktree has changes that cannot be safely resumed")
        paths.append(_safe_git_relative_path(record[3:]))
    return paths


def _nul_paths(output: str) -> list[Path]:
    return [_safe_git_relative_path(value) for value in _nul_records(output)]


def _nul_records(output: str) -> list[str]:
    if not output:
        return []
    if not output.endswith("\0"):
        raise RuntimeError("Git command returned invalid path output")
    records = output[:-1].split("\0")
    if any(not record for record in records):
        raise RuntimeError("Git command returned invalid path output")
    return records


def _safe_git_relative_path(value: str) -> Path:
    if not value or "\\" in value:
        raise RuntimeError("Git command returned invalid path output")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeError("Git command returned invalid path output")
    return path


def _destination_has_bytes(root: Path, relative: Path, expected: bytes) -> bool:
    path = root / relative
    try:
        _validate_chain(root, path.parent, "dashboard target", require_final=True)
    except ValueError:
        return False
    if _is_link_or_reparse(path) or not path.is_file():
        return False
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _git_blob_oid(content: bytes, object_format: str) -> str:
    framed = f"blob {len(content)}\0".encode("ascii") + content
    return hashlib.new(object_format, framed).hexdigest()


def _literal_pathspec(path: str) -> str:
    return f":(literal){path}"


def _index_oids(output: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for record in _nul_records(output):
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if separator != "\t" or len(fields) != 3 or fields[2] != "0":
            raise RuntimeError("Git command ls-files returned invalid output")
        path = _safe_git_relative_path(raw_path).as_posix()
        oid = _object_id(fields[1], "ls-files")
        if path in entries:
            raise RuntimeError("Git command ls-files returned invalid output")
        entries[path] = oid
    return entries


def _tree_oids(output: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for record in _nul_records(output):
        metadata, separator, raw_path = record.partition("\t")
        fields = metadata.split()
        if separator != "\t" or len(fields) != 3 or fields[1] != "blob":
            raise RuntimeError("Git command ls-tree returned invalid output")
        path = _safe_git_relative_path(raw_path).as_posix()
        oid = _object_id(fields[2], "ls-tree")
        if path in entries:
            raise RuntimeError("Git command ls-tree returned invalid output")
        entries[path] = oid
    return entries


def _normalized_git_path(output: str, cwd: Path) -> Path:
    value = _first_line(output).strip()
    if not value:
        raise RuntimeError("Git command rev-parse returned invalid output")
    path = Path(value)
    if not path.is_absolute():
        path = cwd / path
    return path.absolute().resolve(strict=False)


def _listed_worktrees(output: str) -> set[Path]:
    listed: set[Path] = set()
    for field in output.split("\0"):
        if field.startswith("worktree "):
            listed.add(Path(field.removeprefix("worktree ")).absolute())
    return listed


def _object_id(output: str, label: str) -> str:
    value = _first_line(output).strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise RuntimeError(f"Git command {label} returned invalid output")
    return value.casefold()
