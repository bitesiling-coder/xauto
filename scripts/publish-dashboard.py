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
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("publisher Git validation failed")
    return result.stdout.decode("utf-8", errors="strict")


def _git_checked_bytes(git: Path, root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        [str(git), *arguments],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("publisher Git validation failed")
    return result.stdout


def _object_id(value: bytes) -> bytes:
    candidate = value.strip()
    if len(candidate) not in {40, 64} or any(
        byte not in b"0123456789abcdefABCDEF" for byte in candidate
    ):
        raise RuntimeError("publisher Git validation failed")
    return candidate.lower()


def _head_blobs(
    git: Path, root: Path, head: bytes
) -> dict[Path, bytes]:
    expected = set(_TRUSTED_RELATIVE_PATHS)
    tree = _git_checked_bytes(
        git,
        root,
        "ls-tree",
        "-z",
        head.decode("ascii"),
        "--",
        *(path.as_posix() for path in _TRUSTED_RELATIVE_PATHS),
    )
    records = tree[:-1].split(b"\0") if tree.endswith(b"\0") else []
    entries: dict[Path, bytes] = {}
    for record in records:
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise RuntimeError("publisher Git validation failed")
        mode, object_type, raw_oid = fields
        try:
            relative = Path(raw_path.decode("utf-8", errors="strict"))
        except UnicodeDecodeError as error:
            raise RuntimeError("publisher Git validation failed") from error
        if (
            mode != b"100644"
            or object_type != b"blob"
            or relative not in expected
            or relative in entries
        ):
            raise RuntimeError("publisher Git validation failed")
        oid = _object_id(raw_oid)
        entries[relative] = _git_checked_bytes(
            git, root, "cat-file", "blob", oid.decode("ascii")
        )
    if set(entries) != expected:
        raise RuntimeError("publisher Git validation failed")
    return entries


def _validate_index_flags(git: Path, root: Path) -> None:
    output = _git_checked_bytes(
        git,
        root,
        "ls-files",
        "-v",
        "-z",
        "--",
        *(path.as_posix() for path in _TRUSTED_RELATIVE_PATHS),
    )
    records = output[:-1].split(b"\0") if output.endswith(b"\0") else []
    expected = {path.as_posix() for path in _TRUSTED_RELATIVE_PATHS}
    actual: set[str] = set()
    for record in records:
        prefix, separator, raw_path = record.partition(b" ")
        if prefix != b"H" or separator != b" ":
            raise RuntimeError("publisher index flags are unsafe")
        try:
            path = raw_path.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise RuntimeError("publisher index flags are unsafe") from error
        if path not in expected or path in actual:
            raise RuntimeError("publisher index flags are unsafe")
        actual.add(path)
    if actual != expected:
        raise RuntimeError("publisher index flags are unsafe")


def _trusted_root() -> tuple[Path, dict[Path, bytes]]:
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
    head = _object_id(
        _git_checked_bytes(git, root, "rev-parse", "--verify", "HEAD^{commit}")
    )
    blobs = _head_blobs(git, root, head)
    _validate_index_flags(git, root)
    pathspecs = [relative.as_posix() for relative in _TRUSTED_RELATIVE_PATHS]
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
    if script.read_bytes() != blobs[_TRUSTED_RELATIVE_PATHS[0]]:
        raise RuntimeError("trusted publisher wrapper is modified")
    return root, blobs


def _load_source_module(
    name: str,
    path: Path,
    source: bytes,
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
        code = compile(source, str(path), "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _publish() -> dict[str, object]:
    if not (
        sys.flags.isolated
        and sys.flags.no_site
        and sys.flags.ignore_environment
        and sys.flags.safe_path
    ):
        raise RuntimeError("publisher requires isolated Python")
    root, blobs = _trusted_root()
    names = ("xrag", "xrag.public_content", "xrag.dashboard_publish")
    if any(name in sys.modules for name in names):
        raise RuntimeError("publisher module was already loaded")
    package_root = root / "src" / "xrag"
    _load_source_module(
        "xrag",
        package_root / "__init__.py",
        blobs[Path("src/xrag/__init__.py")],
        package="xrag",
        is_package=True,
    )
    _load_source_module(
        "xrag.public_content",
        package_root / "public_content.py",
        blobs[Path("src/xrag/public_content.py")],
        package="xrag",
    )
    module = _load_source_module(
        "xrag.dashboard_publish",
        package_root / "dashboard_publish.py",
        blobs[Path("src/xrag/dashboard_publish.py")],
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
