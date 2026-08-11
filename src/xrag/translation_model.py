from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any, Callable
import uuid


MODEL_ID = "Helsinki-NLP/opus-mt-en-zh"
MANIFEST_VERSION = 1

_MODEL_ERROR = "local translation model unavailable"
_TRANSLATION_ERROR = "local translation failed"
_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_STAGING_PATTERN = re.compile(r"\.install-[0-9a-f]{32}")
_TEMP_MANIFEST_PATTERN = re.compile(r"\.manifest-[0-9a-f]{32}\.tmp")
_MANIFEST_KEYS = {"version", "model_id", "revision", "snapshot", "files"}
_INCOMPLETE_MANIFEST = b'{"version":0,"status":"incomplete"}\n'


@dataclass(frozen=True)
class InstalledTranslationModel:
    model_id: str
    revision: str
    snapshot_path: Path
    files: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if type(self.files) is not tuple:
            raise ValueError("files must be an immutable tuple")
        if any(
            type(item) is not tuple
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
            for item in self.files
        ):
            raise ValueError("files must contain immutable string pairs")
        if self.files != tuple(sorted(self.files)):
            raise ValueError("files must be sorted")


@dataclass(frozen=True)
class _BoundBytes:
    content: bytes
    identity: tuple[int, int, int, int]
    digest: str


@dataclass(frozen=True)
class _TreeNode:
    kind: str
    identity: tuple[int, int, int, int]
    path: Path
    digest: str | None = None


def verify_translation_model(root: Path) -> InstalledTranslationModel:
    try:
        return _verify_translation_model(root)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise RuntimeError(_MODEL_ERROR) from None


def _verify_translation_model(root: Path) -> InstalledTranslationModel:
    resolved_root = _existing_safe_root(root)
    manifest_path = resolved_root / "manifest.json"
    manifest_source = _read_bound_regular_file(manifest_path)
    manifest = _parse_manifest(manifest_source.content)

    revision = manifest["revision"]
    files = manifest["files"]
    assert isinstance(revision, str)
    assert isinstance(files, dict)
    snapshot_path = resolved_root / "snapshots" / revision

    _require_directory(resolved_root / "snapshots")
    actual_files = _hash_snapshot(snapshot_path)
    if set(actual_files) != set(files):
        raise ValueError("snapshot file set differs from manifest")

    verified: list[tuple[str, str]] = []
    for relative in sorted(files):
        expected_hash = files[relative]
        actual_hash = actual_files[relative]
        if actual_hash != expected_hash:
            raise ValueError("snapshot file hash differs from manifest")
        verified.append((relative, expected_hash))

    final_manifest = _read_bound_regular_file(manifest_path)
    if (
        final_manifest.identity != manifest_source.identity
        or final_manifest.digest != manifest_source.digest
    ):
        raise ValueError("manifest changed during verification")

    return InstalledTranslationModel(
        model_id=MODEL_ID,
        revision=revision,
        snapshot_path=snapshot_path.resolve(strict=True),
        files=tuple(verified),
    )


def _parse_manifest(raw: bytes) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result

    manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    if type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("invalid manifest keys")
    if type(manifest["version"]) is not int or manifest["version"] != MANIFEST_VERSION:
        raise ValueError("invalid manifest version")
    if manifest["model_id"] != MODEL_ID:
        raise ValueError("invalid model id")
    revision = manifest["revision"]
    if not isinstance(revision, str) or _REVISION_PATTERN.fullmatch(revision) is None:
        raise ValueError("invalid revision")
    if manifest["snapshot"] != f"snapshots/{revision}":
        raise ValueError("invalid snapshot")
    files = manifest["files"]
    if type(files) is not dict or not files:
        raise ValueError("invalid files")
    for relative, digest in files.items():
        _validate_relative_path(relative)
        if not isinstance(digest, str) or _HASH_PATTERN.fullmatch(digest) is None:
            raise ValueError("invalid file hash")
    return manifest


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("invalid relative path")
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive:
        raise ValueError("invalid relative path")
    return value


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _existing_safe_root(root: Path) -> Path:
    absolute = _absolute_without_resolving(Path(root))
    _require_directory(absolute)
    resolved = absolute.resolve(strict=True)
    _require_directory(resolved)
    return resolved


def _is_reparse(path: Path, info: os.stat_result) -> bool:
    del path
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag) or bool(getattr(info, "st_reparse_tag", 0))


def _require_directory(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(path, info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("unsafe directory")
    return info


def _require_regular_file(path: Path) -> os.stat_result:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(path, info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe file")
    return info


def _walk_tree(root: Path, *, hash_files: bool) -> dict[str, _TreeNode]:
    root_info = _require_directory(root)
    nodes = {".": _TreeNode("directory", _identity(root_info), root)}
    pending = [root]
    while pending:
        directory = pending.pop()
        _require_directory(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                info = os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or _is_reparse(path, info):
                    raise ValueError("unsafe snapshot entry")
                if stat.S_ISDIR(info.st_mode):
                    relative = path.relative_to(root).as_posix()
                    nodes[relative] = _TreeNode("directory", _identity(info), path)
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    relative = path.relative_to(root).as_posix()
                    _validate_relative_path(relative)
                    digest = _hash_file(path) if hash_files else None
                    after = _require_regular_file(path)
                    if _identity(info) != _identity(after):
                        raise ValueError("snapshot entry changed")
                    nodes[relative] = _TreeNode(
                        "file", _identity(after), path, digest
                    )
                else:
                    raise ValueError("special snapshot entry")
    for relative in sorted(
        nodes, key=lambda value: len(PurePosixPath(value).parts), reverse=True
    ):
        node = nodes[relative]
        if node.kind == "directory":
            current = _require_directory(node.path)
        else:
            current = _require_regular_file(node.path)
        if _identity(current) != node.identity:
            raise ValueError("snapshot entry changed")
    return nodes


def _same_tree(first: dict[str, _TreeNode], second: dict[str, _TreeNode]) -> bool:
    return set(first) == set(second) and all(
        first[name].kind == second[name].kind
        and first[name].identity == second[name].identity
        for name in first
    )


def _hash_file(path: Path) -> str:
    before = _require_regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(path, opened)
            or _identity(before) != _identity(opened)
        ):
            raise ValueError("unsafe opened file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            final_handle = os.fstat(source.fileno())
        after = _require_regular_file(path)
        if (
            _identity(opened) != _identity(final_handle)
            or _identity(opened) != _identity(after)
        ):
            raise ValueError("file changed while hashing")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_handle_bytes(source: Any) -> bytes:
    chunks: list[bytes] = []
    while chunk := source.read(1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_bound_regular_file(path: Path) -> _BoundBytes:
    before = _require_regular_file(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(path, opened)
            or _identity(before) != _identity(opened)
        ):
            raise ValueError("unsafe opened file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            content = _read_handle_bytes(source)
            source.seek(0)
            confirmed_content = _read_handle_bytes(source)
            final_handle = os.fstat(source.fileno())
        after = _require_regular_file(path)
        if (
            confirmed_content != content
            or _identity(opened) != _identity(final_handle)
            or _identity(opened) != _identity(after)
        ):
            raise ValueError("file changed while reading")
        return _BoundBytes(
            content=content,
            identity=_identity(opened),
            digest=hashlib.sha256(content).hexdigest(),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino)


def _ownership_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def install_translation_model(
    root: Path,
    *,
    api: object | None = None,
    downloader: Callable[..., object] | None = None,
) -> InstalledTranslationModel:
    try:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi()
        if downloader is None:
            from huggingface_hub import snapshot_download

            downloader = snapshot_download

        info = api.model_info(MODEL_ID, revision="main")  # type: ignore[attr-defined]
        revision = getattr(info, "sha", None)
        if not isinstance(revision, str) or _REVISION_PATTERN.fullmatch(revision) is None:
            raise ValueError("invalid remote revision")

        resolved_root = _prepare_install_root(Path(root))
        from xrag.locking import writer_lock

        with writer_lock(resolved_root, timeout=600):
            return _install_translation_model_transaction(
                resolved_root,
                revision,
                downloader,
            )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise RuntimeError(_MODEL_ERROR) from None


def _install_translation_model_transaction(
    resolved_root: Path,
    revision: str,
    downloader: Callable[..., object],
) -> InstalledTranslationModel:
    staging: Path | None = None
    staging_identity: tuple[int, int] | None = None
    temporary_manifest: Path | None = None
    temporary_manifest_identity: tuple[int, int] | None = None
    rollback_marker: Path | None = None
    rollback_marker_identity: tuple[int, int] | None = None
    try:
        try:
            current = verify_translation_model(resolved_root)
        except RuntimeError:
            current = None
        if current is not None and current.revision == revision:
            return current

        staging = _new_staging_directory(resolved_root)
        staging_identity = _ownership_identity(_require_directory(staging))
        downloader(repo_id=MODEL_ID, revision=revision, local_dir=staging)
        _validate_download_cache(staging)
        staged_files = _hash_snapshot(staging)

        snapshots = resolved_root / "snapshots"
        if not os.path.lexists(snapshots):
            snapshots.mkdir()
        _require_directory(snapshots)
        target = snapshots / revision
        if os.path.lexists(target):
            _require_directory(target)
            if _hash_snapshot(target) != staged_files:
                raise ValueError("existing snapshot differs")
            if not _cleanup_owned_staging(resolved_root, staging, staging_identity):
                raise ValueError("could not clean staging directory")
            staging = None
            staging_identity = None
        else:
            os.replace(staging, target)
            staging = None
            staging_identity = None
            _fsync_directory(snapshots)

        manifest = {
            "version": MANIFEST_VERSION,
            "model_id": MODEL_ID,
            "revision": revision,
            "snapshot": f"snapshots/{revision}",
            "files": dict(sorted(staged_files.items())),
        }
        temporary_manifest = resolved_root / f".manifest-{uuid.uuid4().hex}.tmp"
        if temporary_manifest.parent != resolved_root or _TEMP_MANIFEST_PATTERN.fullmatch(
            temporary_manifest.name
        ) is None:
            raise ValueError("unsafe temporary manifest")
        with temporary_manifest.open("x", encoding="utf-8", newline="\n") as output:
            temporary_manifest_identity = _ownership_identity(
                os.fstat(output.fileno())
            )
            json.dump(manifest, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        _require_regular_file(temporary_manifest)
        manifest_path = resolved_root / "manifest.json"
        previous_manifest, previous_identity = _capture_manifest(manifest_path)
        if previous_identity is None:
            if os.path.lexists(manifest_path):
                raise ValueError("manifest changed before publication")
            rollback_marker, rollback_marker_identity = _prepare_incomplete_marker(
                resolved_root
            )
            _fsync_directory(resolved_root)
            if os.path.lexists(manifest_path):
                raise ValueError("manifest changed before publication")
        elif _identity(_require_regular_file(manifest_path)) != previous_identity:
            raise ValueError("manifest changed before publication")
        published_identity = temporary_manifest_identity
        try:
            os.replace(temporary_manifest, manifest_path)
            temporary_manifest = None
            temporary_manifest_identity = None
            _fsync_directory(resolved_root)
            return verify_translation_model(resolved_root)
        except BaseException:
            _rollback_manifest(
                resolved_root,
                manifest_path,
                published_identity,
                previous_manifest,
                rollback_marker,
                rollback_marker_identity,
            )
            raise
    finally:
        if staging is not None and staging_identity is not None:
            _cleanup_owned_staging(resolved_root, staging, staging_identity)
        if (
            temporary_manifest is not None
            and temporary_manifest_identity is not None
        ):
            _cleanup_owned_manifest(
                resolved_root, temporary_manifest, temporary_manifest_identity
            )


def _prepare_install_root(root: Path) -> Path:
    absolute = _absolute_without_resolving(root)
    if os.path.lexists(absolute):
        _require_directory(absolute)
    else:
        absolute.mkdir(parents=True, exist_ok=True)
        _require_directory(absolute)
    resolved = absolute.resolve(strict=True)
    _require_directory(resolved)
    return resolved


def _new_staging_directory(root: Path) -> Path:
    for _attempt in range(16):
        staging = root / f".install-{uuid.uuid4().hex}"
        if staging.parent != root or _STAGING_PATTERN.fullmatch(staging.name) is None:
            raise ValueError("unsafe staging path")
        try:
            staging.mkdir()
        except FileExistsError:
            continue
        _require_directory(staging)
        return staging
    raise ValueError("could not allocate staging directory")


def _validate_download_cache(staging: Path) -> None:
    cache = staging / ".cache"
    if not os.path.lexists(cache):
        return
    if cache.parent != staging or cache.name != ".cache":
        raise ValueError("unsafe cache path")
    _walk_tree(cache, hash_files=False)


def _hash_snapshot(snapshot: Path) -> dict[str, str]:
    first = _walk_tree(snapshot, hash_files=True)
    second = _walk_tree(snapshot, hash_files=False)
    if not _same_tree(first, second):
        raise ValueError("snapshot changed during verification")
    files = {
        relative: node.digest
        for relative, node in first.items()
        if node.kind == "file"
    }
    if not files or any(digest is None for digest in files.values()):
        raise ValueError("empty snapshot")
    return {relative: files[relative] for relative in sorted(files)}  # type: ignore[return-value]


def _cleanup_owned_staging(
    root: Path,
    staging: Path,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        if (
            staging.parent != root
            or _STAGING_PATTERN.fullmatch(staging.name) is None
            or not os.path.lexists(staging)
        ):
            return False
        return (
            _ownership_identity(_require_directory(staging))
            == expected_identity
        )
    except Exception:
        return False


def _cleanup_owned_manifest(
    root: Path,
    path: Path,
    expected_identity: tuple[int, int],
) -> bool:
    try:
        if (
            path.parent != root
            or _TEMP_MANIFEST_PATTERN.fullmatch(path.name) is None
            or not os.path.lexists(path)
        ):
            return False
        return (
            _ownership_identity(_require_regular_file(path))
            == expected_identity
        )
    except Exception:
        return False


def _capture_manifest(
    path: Path,
) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    if not os.path.lexists(path):
        return None, None
    captured = _read_bound_regular_file(path)
    return captured.content, captured.identity


def _prepare_incomplete_marker(root: Path) -> tuple[Path, tuple[int, int]]:
    for _attempt in range(16):
        candidate = root / f".manifest-{uuid.uuid4().hex}.tmp"
        if (
            candidate.parent != root
            or _TEMP_MANIFEST_PATTERN.fullmatch(candidate.name) is None
        ):
            raise ValueError("unsafe rollback marker")
        try:
            output = candidate.open("xb")
        except FileExistsError:
            continue
        with output:
            marker_identity = _ownership_identity(os.fstat(output.fileno()))
            output.write(_INCOMPLETE_MANIFEST)
            output.flush()
            os.fsync(output.fileno())
        if _ownership_identity(_require_regular_file(candidate)) != marker_identity:
            raise ValueError("rollback marker changed")
        return candidate, marker_identity
    raise ValueError("could not allocate rollback marker")


def _rollback_manifest(
    root: Path,
    manifest_path: Path,
    published_identity: tuple[int, int] | None,
    previous_manifest: bytes | None,
    prepared_path: Path | None,
    prepared_identity: tuple[int, int] | None,
) -> None:
    rollback_path = prepared_path
    rollback_identity = prepared_identity
    try:
        if published_identity is None:
            return
        published = _require_regular_file(manifest_path)
        if _ownership_identity(published) != published_identity:
            return
        if previous_manifest is not None:
            for _attempt in range(16):
                candidate = root / f".manifest-{uuid.uuid4().hex}.tmp"
                if (
                    candidate.parent != root
                    or _TEMP_MANIFEST_PATTERN.fullmatch(candidate.name) is None
                ):
                    return
                try:
                    output = candidate.open("xb")
                except FileExistsError:
                    continue
                rollback_path = candidate
                with output:
                    rollback_identity = _ownership_identity(os.fstat(output.fileno()))
                    output.write(previous_manifest)
                    output.flush()
                    try:
                        os.fsync(output.fileno())
                    except OSError:
                        pass
                break
            if rollback_path is None:
                return
        if rollback_path is None or rollback_identity is None:
            return
        if (
            rollback_path.parent != root
            or _TEMP_MANIFEST_PATTERN.fullmatch(rollback_path.name) is None
            or _ownership_identity(_require_regular_file(rollback_path))
            != rollback_identity
        ):
            return
        current = _require_regular_file(manifest_path)
        if _ownership_identity(current) != published_identity:
            return
        os.replace(rollback_path, manifest_path)
        rollback_path = None
        rollback_identity = None
        try:
            _fsync_directory(root)
        except OSError:
            pass
    except Exception:
        return
    finally:
        if rollback_path is not None and rollback_identity is not None:
            _cleanup_owned_manifest(root, rollback_path, rollback_identity)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TransformersTranslationEngine:
    def __init__(self, model_root: Path) -> None:
        self._model_root = Path(model_root)
        self._installed: InstalledTranslationModel | None = None
        self._fingerprint: tuple[
            tuple[str, str, int, int, int, int], ...
        ] | None = None
        self._tokenizer: object | None = None
        self._model: object | None = None

    @property
    def model_id(self) -> str:
        return MODEL_ID

    @property
    def revision(self) -> str:
        self.preflight()
        assert self._installed is not None
        return self._installed.revision

    def preflight(self) -> None:
        if self._installed is not None and self._fingerprint is not None:
            try:
                current_fingerprint = _model_fingerprint(self._installed)
            except Exception:
                current_fingerprint = None
            if current_fingerprint == self._fingerprint:
                return

        installed = verify_translation_model(self._model_root)
        try:
            fingerprint = _model_fingerprint(installed)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RuntimeError(_MODEL_ERROR) from None

        if self._installed != installed:
            self._tokenizer = None
            self._model = None
        self._installed = installed
        self._fingerprint = fingerprint

    def _load(self) -> None:
        if self._tokenizer is not None and self._model is not None:
            return
        import torch  # noqa: F401
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        installed = verify_translation_model(self._model_root)
        if self._installed != installed:
            self._tokenizer = None
            self._model = None
        path = str(installed.snapshot_path)
        tokenizer = AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        model = AutoModelForSeq2SeqLM.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        model.to("cpu")
        model.eval()
        fingerprint = _model_fingerprint(installed)
        self._installed = installed
        self._fingerprint = fingerprint
        self._tokenizer = tokenizer
        self._model = model

    def translate_many(self, texts: Sequence[str]) -> list[str]:
        try:
            if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence):
                raise ValueError("invalid translation batch")
            values = list(texts)
            if not values:
                return []
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("invalid translation input")

            self.preflight()
            self._load()
            assert self._tokenizer is not None and self._model is not None
            import torch

            encoded = self._tokenizer(
                values,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            with torch.inference_mode():
                generated = self._model.generate(
                    **encoded,
                    do_sample=False,
                    num_beams=4,
                    max_new_tokens=512,
                )
            decoded = self._tokenizer.batch_decode(
                generated, skip_special_tokens=True
            )
            if type(decoded) is not list or len(decoded) != len(values):
                raise ValueError("invalid translation result")
            if any(not isinstance(value, str) or not value.strip() for value in decoded):
                raise ValueError("invalid translation result")
            return [value.strip() for value in decoded]
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise RuntimeError(_TRANSLATION_ERROR) from None


def _model_fingerprint(
    installed: InstalledTranslationModel,
) -> tuple[tuple[str, str, int, int, int, int], ...]:
    root = installed.snapshot_path.parents[1]
    entries: list[tuple[str, str, int, int, int, int]] = []

    def add(
        label: str,
        kind: str,
        path: Path,
        require: Callable[[Path], os.stat_result],
    ) -> None:
        size, mtime_ns, device, inode = _identity(require(path))
        entries.append((label, kind, device, inode, size, mtime_ns))

    add("manifest.json", "file", root / "manifest.json", _require_regular_file)
    add("snapshots", "directory", root / "snapshots", _require_directory)
    tree = _walk_tree(installed.snapshot_path, hash_files=False)
    for relative in sorted(tree):
        node = tree[relative]
        size, mtime_ns, device, inode = node.identity
        entries.append((relative, node.kind, device, inode, size, mtime_ns))
    return tuple(entries)
