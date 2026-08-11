from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
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
    _require_regular_file(manifest_path)
    manifest = _read_manifest(manifest_path)

    revision = manifest["revision"]
    files = manifest["files"]
    assert isinstance(revision, str)
    assert isinstance(files, dict)
    snapshot_path = resolved_root / "snapshots" / revision

    _require_directory(resolved_root / "snapshots")
    _require_directory(snapshot_path)
    actual_paths = _walk_regular_files(snapshot_path)
    if set(actual_paths) != set(files):
        raise ValueError("snapshot file set differs from manifest")

    verified: list[tuple[str, str]] = []
    for relative in sorted(files):
        expected_hash = files[relative]
        actual_hash = _hash_file(actual_paths[relative])
        if actual_hash != expected_hash:
            raise ValueError("snapshot file hash differs from manifest")
        verified.append((relative, expected_hash))

    return InstalledTranslationModel(
        model_id=MODEL_ID,
        revision=revision,
        snapshot_path=snapshot_path.resolve(strict=True),
        files=tuple(verified),
    )


def _read_manifest(path: Path) -> dict[str, Any]:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result

    raw = path.read_text(encoding="utf-8")
    manifest = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
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


def _walk_regular_files(root: Path) -> dict[str, Path]:
    _require_directory(root)
    files: dict[str, Path] = {}
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
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    relative = path.relative_to(root).as_posix()
                    _validate_relative_path(relative)
                    files[relative] = path
                else:
                    raise ValueError("special snapshot entry")
    return files


def _hash_file(path: Path) -> str:
    before = _require_regular_file(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if not stat.S_ISREG(opened.st_mode) or _is_reparse(path, opened):
            raise ValueError("unsafe opened file")
        if _identity(before) != _identity(opened):
            raise ValueError("file changed before hashing")
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    after = _require_regular_file(path)
    if _identity(before) != _identity(after):
        raise ValueError("file changed while hashing")
    return digest.hexdigest()


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
        _remove_download_cache(staging)
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
        elif _identity(_require_regular_file(manifest_path)) != previous_identity:
            raise ValueError("manifest changed before publication")
        published_identity = temporary_manifest_identity
        os.replace(temporary_manifest, manifest_path)
        temporary_manifest = None
        temporary_manifest_identity = None
        try:
            _fsync_directory(resolved_root)
            return verify_translation_model(resolved_root)
        except BaseException:
            _rollback_manifest(
                resolved_root,
                manifest_path,
                published_identity,
                previous_manifest,
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


def _remove_download_cache(staging: Path) -> None:
    cache = staging / ".cache"
    if not os.path.lexists(cache):
        return
    if cache.parent != staging or cache.name != ".cache":
        raise ValueError("unsafe cache path")
    _require_directory(cache)
    _walk_regular_files(cache)
    shutil.rmtree(cache)


def _hash_snapshot(snapshot: Path) -> dict[str, str]:
    paths = _walk_regular_files(snapshot)
    if not paths:
        raise ValueError("empty snapshot")
    return {relative: _hash_file(paths[relative]) for relative in sorted(paths)}


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
        info = _require_directory(staging)
        if _ownership_identity(info) != expected_identity:
            return False
        _walk_regular_files(staging)
        shutil.rmtree(staging)
        return not os.path.lexists(staging)
    except Exception:
        return False


def _cleanup_owned_manifest(
    root: Path,
    path: Path,
    expected_identity: tuple[int, int],
) -> None:
    try:
        if (
            path.parent != root
            or _TEMP_MANIFEST_PATTERN.fullmatch(path.name) is None
            or not os.path.lexists(path)
        ):
            return
        info = _require_regular_file(path)
        if _ownership_identity(info) == expected_identity:
            path.unlink()
    except Exception:
        return


def _capture_manifest(
    path: Path,
) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    if not os.path.lexists(path):
        return None, None
    before = _require_regular_file(path)
    contents = path.read_bytes()
    after = _require_regular_file(path)
    if _identity(before) != _identity(after):
        raise ValueError("manifest changed while reading")
    return contents, _identity(after)


def _rollback_manifest(
    root: Path,
    manifest_path: Path,
    published_identity: tuple[int, int] | None,
    previous_manifest: bytes | None,
) -> None:
    rollback_path: Path | None = None
    rollback_identity: tuple[int, int] | None = None
    try:
        if published_identity is None:
            return
        current = _require_regular_file(manifest_path)
        if _ownership_identity(current) != published_identity:
            return
        if previous_manifest is None:
            manifest_path.unlink()
        else:
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
        self._fingerprint: tuple[tuple[str, int, int, int, int], ...] | None = None
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
        fingerprint = _model_fingerprint(installed)
        if self._installed != installed:
            self._tokenizer = None
            self._model = None
        self._installed = installed
        self._fingerprint = fingerprint
        path = str(self._installed.snapshot_path)
        tokenizer = AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        model = AutoModelForSeq2SeqLM.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        model.to("cpu")
        model.eval()
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
) -> tuple[tuple[str, int, int, int, int], ...]:
    root = installed.snapshot_path.parents[1]
    entries: list[tuple[str, int, int, int, int]] = []

    def add(label: str, path: Path, require: Callable[[Path], os.stat_result]) -> None:
        size, mtime_ns, device, inode = _identity(require(path))
        entries.append((label, size, mtime_ns, device, inode))

    add("manifest.json", root / "manifest.json", _require_regular_file)
    add("snapshots", root / "snapshots", _require_directory)
    add(f"snapshots/{installed.revision}", installed.snapshot_path, _require_directory)
    directories: set[str] = set()
    for relative, _digest in installed.files:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            directories.add(parent.as_posix())
            parent = parent.parent
    for relative in sorted(directories):
        add(f"dir:{relative}", installed.snapshot_path.joinpath(*relative.split("/")), _require_directory)
    for relative, _digest in installed.files:
        add(relative, installed.snapshot_path.joinpath(*relative.split("/")), _require_regular_file)
    return tuple(entries)
