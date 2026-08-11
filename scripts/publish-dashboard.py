from __future__ import annotations

import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import types


_ERROR = "Error: dashboard publication failed"
_TRUSTED_RELATIVE_PATHS = (
    Path("scripts/publish-dashboard.py"),
    Path("src/xrag/__init__.py"),
    Path("src/xrag/dashboard_publish.py"),
    Path("src/xrag/public_content.py"),
)


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


def _validate_real_path(root: Path, path: Path, *, directory: bool = False) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_reparse(current) or current.resolve(strict=True) != current:
            raise RuntimeError("untrusted publisher path")
    if directory:
        if not path.is_dir():
            raise RuntimeError("untrusted publisher path")
    elif not path.is_file():
        raise RuntimeError("untrusted publisher path")


def _git_checked(git: Path, root: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(git), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("publisher Git validation failed")
    return result.stdout


def _trusted_root() -> Path:
    if sys.platform != "win32":
        raise RuntimeError("publisher requires Windows Python")
    invoked = Path(__file__).absolute()
    script = Path(__file__).resolve(strict=True)
    if invoked != script or _is_link_or_reparse(script):
        raise RuntimeError("untrusted publisher path")
    root = script.parent.parent
    if root.resolve(strict=True) != root or _is_link_or_reparse(root):
        raise RuntimeError("untrusted publisher path")
    if script != root / _TRUSTED_RELATIVE_PATHS[0]:
        raise RuntimeError("untrusted publisher path")

    for relative in _TRUSTED_RELATIVE_PATHS:
        _validate_real_path(root, root / relative)
    git_marker = root / ".git"
    _validate_real_path(root, git_marker, directory=git_marker.is_dir())

    git_value = shutil.which("git.exe")
    if not git_value:
        raise RuntimeError("Windows Git is unavailable")
    git = Path(git_value).resolve(strict=True)
    if not git.is_file() or _is_link_or_reparse(git):
        raise RuntimeError("Windows Git is unavailable")

    top_output = _git_checked(git, root, "rev-parse", "--show-toplevel")
    top_lines = top_output.splitlines()
    if len(top_lines) != 1 or Path(top_lines[0]).resolve(strict=True) != root:
        raise RuntimeError("untrusted publisher repository")
    pathspecs = [relative.as_posix() for relative in _TRUSTED_RELATIVE_PATHS]
    _git_checked(git, root, "ls-files", "--error-unmatch", "--", *pathspecs)
    status = _git_checked(
        git,
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=no",
        "--",
        *pathspecs,
    )
    if status:
        raise RuntimeError("trusted publisher code is modified")
    return root


def _load_source_module(
    name: str,
    path: Path,
    *,
    package: str,
    is_package: bool = False,
) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = package
    if is_package:
        module.__path__ = []
    sys.modules[name] = module
    try:
        code = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _publish() -> dict[str, object]:
    root = _trusted_root()
    names = ("xrag", "xrag.public_content", "xrag.dashboard_publish")
    if any(name in sys.modules for name in names):
        raise RuntimeError("publisher module was already loaded")
    package_root = root / "src" / "xrag"
    _load_source_module(
        "xrag",
        package_root / "__init__.py",
        package="xrag",
        is_package=True,
    )
    _load_source_module(
        "xrag.public_content",
        package_root / "public_content.py",
        package="xrag",
    )
    module = _load_source_module(
        "xrag.dashboard_publish",
        package_root / "dashboard_publish.py",
        package="xrag",
    )
    publisher = module.PagesPublisher(
        root, root / ".worktrees" / "x-rag-pages"
    )
    return publisher.publish(root / "data" / "dashboard-site")


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = sys.argv[1:] if argv is None else argv
        if arguments:
            raise ValueError("arguments are not accepted")
        result = _publish()
        output = json.dumps(result, ensure_ascii=False, default=str)
    except BaseException:
        print(_ERROR, file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
