from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.translation_model import (
    MANIFEST_VERSION,
    MODEL_ID,
    InstalledTranslationModel,
    TransformersTranslationEngine,
    install_translation_model,
    verify_translation_model,
)


REVISION_A = "a" * 40
REVISION_B = "b" * 40


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_snapshot(
    root: Path,
    revision: str = REVISION_A,
    files: dict[str, bytes] | None = None,
    *,
    publish_manifest: bool = True,
) -> dict[str, str]:
    contents = files or {
        "config.json": b"{}",
        "tokenizer.json": b'{"tokenizer": true}',
        "model.safetensors": b"model-weights",
    }
    snapshot = root / "snapshots" / revision
    for relative, data in contents.items():
        destination = snapshot.joinpath(*relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    hashes = {relative: _sha(data) for relative, data in contents.items()}
    if publish_manifest:
        _write_manifest(root, revision, hashes)
    return hashes


def _write_manifest(
    root: Path,
    base_revision: str,
    base_files: dict[str, str],
    **changes: object,
) -> None:
    manifest: dict[str, object] = {
        "version": MANIFEST_VERSION,
        "model_id": MODEL_ID,
        "revision": base_revision,
        "snapshot": f"snapshots/{base_revision}",
        "files": base_files,
    }
    manifest.update(changes)
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )


def _symlink_or_skip(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlinks unavailable: {error}")


class FakeApi:
    def __init__(self, sha: object) -> None:
        self.sha = sha
        self.calls: list[tuple[str, str]] = []

    def model_info(self, model_id: str, *, revision: str) -> object:
        self.calls.append((model_id, revision))
        return SimpleNamespace(sha=self.sha)


def _downloader_for(
    files: dict[str, bytes],
    calls: list[dict[str, object]] | None = None,
) -> Callable[..., None]:
    def download(**kwargs: object) -> None:
        if calls is not None:
            calls.append(kwargs)
        local_dir = Path(kwargs["local_dir"])  # type: ignore[arg-type]
        for relative, data in files.items():
            destination = local_dir.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

    return download


def test_installed_result_is_frozen_deeply_immutable_and_sorted(
    tmp_path: Path,
) -> None:
    _write_snapshot(
        tmp_path,
        files={"z.bin": b"z", "nested/a.bin": b"a"},
    )

    installed = verify_translation_model(tmp_path)

    assert installed == InstalledTranslationModel(
        model_id=MODEL_ID,
        revision=REVISION_A,
        snapshot_path=(tmp_path / "snapshots" / REVISION_A).resolve(),
        files=(("nested/a.bin", _sha(b"a")), ("z.bin", _sha(b"z"))),
    )
    assert type(installed.files) is tuple
    assert all(type(item) is tuple for item in installed.files)
    with pytest.raises(FrozenInstanceError):
        installed.revision = REVISION_B
    with pytest.raises(TypeError):
        installed.files[0] = ("changed", "0" * 64)


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"model_id": "private/secret-model"},
        {"revision": "A" * 40},
        {"revision": "a" * 39},
        {"snapshot": f"snapshots/{REVISION_B}"},
        {"extra": "not allowed"},
        {"files": {}},
        {"files": {"config.json": "A" * 64}},
    ],
)
def test_verify_rejects_invalid_manifest_schema(
    tmp_path: Path, change: dict[str, object]
) -> None:
    hashes = _write_snapshot(tmp_path)
    _write_manifest(tmp_path, REVISION_A, hashes, **change)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


@pytest.mark.parametrize(
    "relative",
    [
        "../escape",
        "nested/../escape",
        "nested//file",
        "./file",
        "/absolute",
        "C:/drive",
        "back\\slash",
        "nul\x00byte",
        "",
    ],
)
def test_verify_rejects_unsafe_manifest_file_paths(
    tmp_path: Path, relative: str
) -> None:
    _write_snapshot(tmp_path)
    _write_manifest(tmp_path, REVISION_A, {relative: "0" * 64})

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


@pytest.mark.parametrize("mutation", ["tamper", "missing", "extra"])
def test_verify_rejects_tampered_missing_or_unlisted_files(
    tmp_path: Path, mutation: str
) -> None:
    _write_snapshot(tmp_path)
    snapshot = tmp_path / "snapshots" / REVISION_A
    if mutation == "tamper":
        (snapshot / "model.safetensors").write_bytes(b"private-tamper")
    elif mutation == "missing":
        (snapshot / "model.safetensors").unlink()
    else:
        (snapshot / "private-extra.bin").write_bytes(b"extra")

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_verify_rejects_symlinked_manifest_file(tmp_path: Path) -> None:
    hashes = _write_snapshot(tmp_path)
    snapshot = tmp_path / "snapshots" / REVISION_A
    target = snapshot / "real.bin"
    target.write_bytes(b"outside")
    link = snapshot / "linked.bin"
    _symlink_or_skip(target, link)
    hashes["real.bin"] = _sha(b"outside")
    hashes["linked.bin"] = _sha(b"outside")
    _write_manifest(tmp_path, REVISION_A, hashes)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_verify_rejects_symlinked_snapshot_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.json").write_bytes(b"{}")
    (tmp_path / "snapshots").mkdir()
    _symlink_or_skip(outside, tmp_path / "snapshots" / REVISION_A, directory=True)
    _write_manifest(tmp_path, REVISION_A, {"config.json": _sha(b"{}")})

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_verify_rejects_simulated_windows_reparse_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path)
    original = module._is_reparse
    monkeypatch.setattr(
        module,
        "_is_reparse",
        lambda path, info: path.name == "model.safetensors" or original(path, info),
    )

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unsupported")
def test_verify_rejects_special_file(tmp_path: Path) -> None:
    hashes = _write_snapshot(tmp_path)
    fifo = tmp_path / "snapshots" / REVISION_A / "pipe"
    try:
        os.mkfifo(fifo)
    except OSError as exc:
        pytest.skip(f"filesystem does not support FIFO test fixture: {exc.errno}")
    hashes["pipe"] = "0" * 64
    _write_manifest(tmp_path, REVISION_A, hashes)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_verification_errors_redact_paths_and_manifest_content(tmp_path: Path) -> None:
    secret = "VERY-PRIVATE-MANIFEST-CONTENT"
    (tmp_path / "manifest.json").write_text(secret, encoding="utf-8")

    with pytest.raises(RuntimeError) as caught:
        verify_translation_model(tmp_path)

    assert str(caught.value) == "local translation model unavailable"
    assert secret not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("mutation", ["identity", "content"])
def test_verify_binds_manifest_handle_to_final_path_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path)
    manifest = tmp_path / "manifest.json"
    original_bytes = manifest.read_bytes()
    original_stat = manifest.stat()
    replacement = tmp_path / "replacement-manifest.json"
    replacement.write_bytes(original_bytes)
    real_read = module._read_handle_bytes
    swapped = False

    def swapping_read(source: object) -> bytes:
        nonlocal swapped
        data = real_read(source)
        if not swapped:
            swapped = True
            if mutation == "identity":
                os.replace(replacement, manifest)
            else:
                changed = original_bytes.replace(b" ", b"\t", 1)
                assert changed != original_bytes and len(changed) == len(original_bytes)
                manifest.write_bytes(changed)
                os.utime(
                    manifest,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
        return data

    monkeypatch.setattr(module, "_read_handle_bytes", swapping_read)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


@pytest.mark.parametrize("swap_kind", ["file", "directory"])
def test_verify_rechecks_entire_tree_after_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap_kind: str
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, files={"nested/config.json": b"same"})
    snapshot = tmp_path / "snapshots" / REVISION_A
    real_hash = module._hash_file
    swapped = False

    def swapping_hash(path: Path) -> str:
        nonlocal swapped
        digest = real_hash(path)
        if not swapped:
            swapped = True
            if swap_kind == "file":
                replacement = tmp_path / "replacement-file"
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)
            else:
                original = snapshot / "nested"
                parked = tmp_path / "parked-original-directory"
                replacement = tmp_path / "replacement-directory"
                replacement.mkdir()
                (replacement / "config.json").write_bytes(b"same")
                os.replace(original, parked)
                os.replace(replacement, original)
        return digest

    monkeypatch.setattr(module, "_hash_file", swapping_hash)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_install_pins_revision_publishes_manifest_last_and_roundtrips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    calls: list[dict[str, object]] = []
    replacements: list[str] = []
    real_replace = module.os.replace

    def recording_replace(source: object, destination: object) -> None:
        replacements.append(Path(destination).name)
        real_replace(source, destination)

    monkeypatch.setattr(module.os, "replace", recording_replace)
    api = FakeApi(REVISION_A)
    installed = install_translation_model(
        tmp_path,
        api=api,
        downloader=_downloader_for({"config.json": b"{}"}, calls),
    )

    assert api.calls == [(MODEL_ID, "main")]
    assert calls == [
        {"repo_id": MODEL_ID, "revision": REVISION_A, "local_dir": calls[0]["local_dir"]}
    ]
    assert Path(calls[0]["local_dir"]).parent == tmp_path.resolve()
    assert Path(calls[0]["local_dir"]).name.startswith(".install-")
    assert replacements[-1] == "manifest.json"
    assert replacements[-2] == REVISION_A
    assert installed == verify_translation_model(tmp_path)
    assert installed.files == (("config.json", _sha(b"{}")),)
    audit_markers = list(tmp_path.glob(".manifest-*.tmp"))
    assert len(audit_markers) == 1
    assert audit_markers[0].read_bytes() == b'{"version":0,"status":"incomplete"}\n'


def test_install_same_revision_is_idempotent_without_downloading(tmp_path: Path) -> None:
    _write_snapshot(tmp_path)

    installed = install_translation_model(
        tmp_path,
        api=FakeApi(REVISION_A),
        downloader=lambda **_kwargs: pytest.fail("downloader must not run"),
    )

    assert installed.revision == REVISION_A


def test_install_new_revision_preserves_old_snapshot(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, REVISION_A)
    old_file = tmp_path / "snapshots" / REVISION_A / "model.safetensors"

    installed = install_translation_model(
        tmp_path,
        api=FakeApi(REVISION_B),
        downloader=_downloader_for({"config.json": b"new"}),
    )

    assert installed.revision == REVISION_B
    assert old_file.read_bytes() == b"model-weights"


def test_install_download_failure_preserves_current_manifest_and_sentinels(
    tmp_path: Path,
) -> None:
    _write_snapshot(tmp_path, REVISION_A)
    manifest_before = (tmp_path / "manifest.json").read_bytes()
    sentinel = tmp_path / ".install-sentinel"
    sentinel.mkdir()
    (sentinel / "keep").write_text("keep", encoding="utf-8")

    def fail_download(**kwargs: object) -> None:
        staging = Path(kwargs["local_dir"])  # type: ignore[arg-type]
        (staging / "partial.bin").write_bytes(b"partial")
        current = staging.stat()
        os.utime(
            staging,
            ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
        )
        raise OSError("SECRET download failure")

    with pytest.raises(RuntimeError) as caught:
        install_translation_model(
            tmp_path, api=FakeApi(REVISION_B), downloader=fail_download
        )

    assert str(caught.value) == "local translation model unavailable"
    assert "SECRET" not in str(caught.value)
    assert (tmp_path / "manifest.json").read_bytes() == manifest_before
    assert (sentinel / "keep").read_text(encoding="utf-8") == "keep"
    preserved = [path for path in tmp_path.glob(".install-*") if path != sentinel]
    assert len(preserved) == 1
    assert (preserved[0] / "partial.bin").read_bytes() == b"partial"


def test_install_manifest_fsync_failure_cleans_only_own_temp_and_preserves_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, REVISION_A)
    manifest_before = (tmp_path / "manifest.json").read_bytes()
    real_fsync = module.os.fsync

    def fail_file_fsync(descriptor: int) -> None:
        opened = module._ownership_identity(os.fstat(descriptor))
        for candidate in tmp_path.glob(".manifest-*.tmp"):
            if module._ownership_identity(candidate.stat()) == opened:
                raise OSError("SECRET fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(
        module.os,
        "fsync",
        fail_file_fsync,
    )

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_B),
            downloader=_downloader_for({"config.json": b"new"}),
        )

    assert (tmp_path / "manifest.json").read_bytes() == manifest_before
    preserved = list(tmp_path.glob(".manifest-*.tmp"))
    assert len(preserved) == 1
    assert b'"revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"' in preserved[0].read_bytes()


def test_install_post_publish_failure_rolls_back_current_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, REVISION_A)
    manifest_before = (tmp_path / "manifest.json").read_bytes()
    real_fsync_directory = module._fsync_directory

    def fail_root_fsync(path: Path) -> None:
        if Path(path) == tmp_path.resolve():
            raise OSError("SECRET post-publish failure")
        real_fsync_directory(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_root_fsync)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_B),
            downloader=_downloader_for({"config.json": b"new"}),
        )

    assert (tmp_path / "manifest.json").read_bytes() == manifest_before
    assert verify_translation_model(tmp_path).revision == REVISION_A
    assert not list(tmp_path.glob(".manifest-*.tmp"))


def test_first_install_post_publish_fsync_failure_leaves_inactive_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    real_fsync_directory = module._fsync_directory
    injected = False

    def fail_root_fsync(path: Path) -> None:
        nonlocal injected
        if Path(path) == tmp_path.resolve() and (tmp_path / "manifest.json").exists():
            injected = True
            raise OSError("SECRET post-publish failure")
        real_fsync_directory(path)

    monkeypatch.setattr(module, "_fsync_directory", fail_root_fsync)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_A),
            downloader=_downloader_for({"config.json": b"new"}),
        )

    assert injected is True
    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        verify_translation_model(tmp_path)


def test_first_install_final_verify_failure_leaves_inactive_manifest_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    real_verify = module.verify_translation_model
    injected = False

    def fail_first_valid_manifest(root: Path) -> InstalledTranslationModel:
        nonlocal injected
        installed = real_verify(root)
        if not injected:
            injected = True
            raise RuntimeError("SECRET final verification failure")
        return installed

    monkeypatch.setattr(module, "verify_translation_model", fail_first_valid_manifest)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_A),
            downloader=_downloader_for({"config.json": b"new"}),
        )

    assert injected is True
    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        real_verify(tmp_path)

    monkeypatch.setattr(module, "verify_translation_model", real_verify)
    installed = install_translation_model(
        tmp_path,
        api=FakeApi(REVISION_A),
        downloader=_downloader_for({"config.json": b"new"}),
    )

    assert installed.revision == REVISION_A
    assert real_verify(tmp_path) == installed


def test_install_rolls_back_when_manifest_replace_succeeds_then_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, REVISION_A)
    manifest_before = (tmp_path / "manifest.json").read_bytes()
    real_replace = module.os.replace
    injected = False

    def replace_then_fail(source: object, destination: object) -> None:
        nonlocal injected
        real_replace(source, destination)
        if Path(destination).name == "manifest.json" and not injected:
            injected = True
            raise OSError("SECRET replace completion ambiguity")

    monkeypatch.setattr(module.os, "replace", replace_then_fail)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_B),
            downloader=_downloader_for({"config.json": b"new"}),
        )

    assert injected is True
    assert (tmp_path / "manifest.json").read_bytes() == manifest_before
    assert verify_translation_model(tmp_path).revision == REVISION_A


@pytest.mark.parametrize("mutation", ["identity", "content"])
def test_capture_manifest_rejects_path_swap_after_bound_handle_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path)
    manifest = tmp_path / "manifest.json"
    original_bytes = manifest.read_bytes()
    original_stat = manifest.stat()
    replacement = tmp_path / "replacement-manifest"
    replacement.write_bytes(original_bytes)
    real_read = module._read_handle_bytes
    swapped = False

    def swapping_read(source: object) -> bytes:
        nonlocal swapped
        data = real_read(source)
        if not swapped:
            swapped = True
            if mutation == "identity":
                os.replace(replacement, manifest)
            else:
                changed = original_bytes.replace(b" ", b"\t", 1)
                assert changed != original_bytes and len(changed) == len(original_bytes)
                manifest.write_bytes(changed)
                os.utime(
                    manifest,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
        return data

    monkeypatch.setattr(module, "_read_handle_bytes", swapping_read)

    with pytest.raises((ValueError, OSError)):
        module._capture_manifest(manifest)

    assert swapped is True


def test_install_holds_per_root_writer_lock_across_all_replacements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.locking as locking
    import xrag.translation_model as module

    state = {"active": False, "entries": 0}

    @contextmanager
    def checking_lock(root: Path, timeout: float) -> object:
        assert Path(root) == tmp_path.resolve()
        assert timeout > 0
        assert state["active"] is False
        state["active"] = True
        state["entries"] += 1
        try:
            yield
        finally:
            state["active"] = False

    real_replace = module.os.replace

    def guarded_replace(source: object, destination: object) -> None:
        assert state["active"] is True
        real_replace(source, destination)

    monkeypatch.setattr(locking, "writer_lock", checking_lock)
    monkeypatch.setattr(module.os, "replace", guarded_replace)

    install_translation_model(
        tmp_path,
        api=FakeApi(REVISION_A),
        downloader=_downloader_for({"config.json": b"new"}),
    )

    assert state == {"active": False, "entries": 1}


def test_install_reuses_verified_model_without_remote_api_or_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(tmp_path, REVISION_A)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")

    class OfflineApi:
        def model_info(self, *_args: object, **_kwargs: object) -> object:
            pytest.fail("verified local model must not query the remote API")

    def offline_downloader(**_kwargs: object) -> None:
        pytest.fail("verified local model must not download")

    installed = install_translation_model(
        tmp_path, api=OfflineApi(), downloader=offline_downloader
    )

    assert installed.revision == REVISION_A
    assert installed.files == (
        ("config.json", _sha(b"{}")),
        ("model.safetensors", _sha(b"model-weights")),
        ("tokenizer.json", _sha(b'{"tokenizer": true}')),
    )


@pytest.mark.parametrize("same", [True, False])
def test_install_reuses_only_exact_existing_target(tmp_path: Path, same: bool) -> None:
    target_files = {"config.json": b"same" if same else b"different"}
    _write_snapshot(
        tmp_path,
        REVISION_A,
        target_files,
        publish_manifest=False,
    )

    if same:
        installed = install_translation_model(
            tmp_path,
            api=FakeApi(REVISION_A),
            downloader=_downloader_for({"config.json": b"same"}),
        )
        assert installed.files == (("config.json", _sha(b"same")),)
        preserved = list(tmp_path.glob(".install-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "config.json").read_bytes() == b"same"
    else:
        with pytest.raises(
            RuntimeError, match="^local translation model unavailable$"
        ):
            install_translation_model(
                tmp_path,
                api=FakeApi(REVISION_A),
                downloader=_downloader_for({"config.json": b"same"}),
            )
        assert not (tmp_path / "manifest.json").exists()
        assert (tmp_path / "snapshots" / REVISION_A / "config.json").read_bytes() == b"different"
        preserved = list(tmp_path.glob(".install-*"))
        assert len(preserved) == 1
        assert (preserved[0] / "config.json").read_bytes() == b"same"


def test_install_includes_safe_download_cache_in_verified_snapshot(tmp_path: Path) -> None:
    def downloader(**kwargs: object) -> None:
        staging = Path(kwargs["local_dir"])  # type: ignore[arg-type]
        (staging / ".cache" / "huggingface").mkdir(parents=True)
        (staging / ".cache" / "huggingface" / "meta").write_text(
            "cache", encoding="utf-8"
        )
        (staging / "config.json").write_bytes(b"{}")

    installed = install_translation_model(
        tmp_path, api=FakeApi(REVISION_A), downloader=downloader
    )

    assert installed.files == (
        (".cache/huggingface/meta", _sha(b"cache")),
        ("config.json", _sha(b"{}")),
    )
    assert (installed.snapshot_path / ".cache" / "huggingface" / "meta").read_text(
        encoding="utf-8"
    ) == "cache"


def test_install_rejects_unsafe_download_cache_and_retains_staging(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.write_text("PRIVATE-KEEP", encoding="utf-8")

    def downloader(**kwargs: object) -> None:
        staging = Path(kwargs["local_dir"])  # type: ignore[arg-type]
        cache = staging / ".cache"
        cache.mkdir()
        (staging / "config.json").write_bytes(b"{}")
        _symlink_or_skip(outside, cache / "unsafe-link")

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path, api=FakeApi(REVISION_A), downloader=downloader
        )

    preserved = list(tmp_path.glob(".install-*"))
    assert len(preserved) == 1
    assert preserved[0].is_dir()
    assert (preserved[0] / ".cache" / "unsafe-link").is_symlink()
    assert outside.read_text(encoding="utf-8") == "PRIVATE-KEEP"


@pytest.mark.parametrize("sha", [None, "A" * 40, "a" * 39, 123])
def test_install_rejects_invalid_api_revision_without_downloading(
    tmp_path: Path, sha: object
) -> None:
    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            tmp_path,
            api=FakeApi(sha),
            downloader=lambda **_kwargs: pytest.fail("downloader must not run"),
        )


def test_install_rejects_symlink_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    root = tmp_path / "linked-root"
    _symlink_or_skip(real, root, directory=True)

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        install_translation_model(
            root,
            api=FakeApi(REVISION_A),
            downloader=lambda **_kwargs: pytest.fail("downloader must not run"),
        )


def test_staging_cleanup_never_deletes_swapped_unowned_directory(
    tmp_path: Path,
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'1' * 32}"
    staging.mkdir()
    (staging / "owned").write_text("owned", encoding="utf-8")
    identity = module._ownership_identity(module._require_directory(staging))
    unowned = tmp_path / "unowned-staging-sentinel"
    unowned.mkdir()
    sentinel = unowned / "PRIVATE-KEEP"
    sentinel.write_text("keep", encoding="utf-8")
    parked = tmp_path / "parked-owned-staging"
    os.replace(staging, parked)
    os.replace(unowned, staging)

    assert module._cleanup_owned_staging(tmp_path, staging, identity) is False

    assert (staging / sentinel.name).read_text(encoding="utf-8") == "keep"
    assert (parked / "owned").read_text(encoding="utf-8") == "owned"


def test_safe_download_cache_validation_does_not_move_or_delete_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'2' * 32}"
    cache = staging / ".cache"
    cache.mkdir(parents=True)
    (cache / "owned").write_text("owned", encoding="utf-8")
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args, **_kwargs: pytest.fail("cache validation must not move files"),
    )

    module._validate_download_cache(staging)

    assert (cache / "owned").read_text(encoding="utf-8") == "owned"


def test_temp_manifest_cleanup_never_deletes_swapped_unowned_file(
    tmp_path: Path,
) -> None:
    import xrag.translation_model as module

    temporary = tmp_path / f".manifest-{'3' * 32}.tmp"
    temporary.write_text("owned", encoding="utf-8")
    identity = module._ownership_identity(module._require_regular_file(temporary))
    unowned = tmp_path / "unowned-manifest-sentinel"
    unowned.write_text("PRIVATE-KEEP", encoding="utf-8")
    parked = tmp_path / "parked-owned-manifest"
    os.replace(temporary, parked)
    os.replace(unowned, temporary)

    assert module._cleanup_owned_manifest(tmp_path, temporary, identity) is False

    assert temporary.read_text(encoding="utf-8") == "PRIVATE-KEEP"
    assert parked.read_text(encoding="utf-8") == "owned"


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_cleanup_preserves_owned_tree_in_place_without_scanning_other_temps(
    tmp_path: Path, entry_kind: str
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'4' * 32}"
    staging.mkdir()
    owned = staging / "owned"
    if entry_kind == "file":
        owned.write_text("owned", encoding="utf-8")
    else:
        owned.mkdir()
        (owned / "payload").write_text("owned", encoding="utf-8")
    unrelated = tmp_path / f".retained-{'9' * 32}"
    unrelated.mkdir()
    (unrelated / "PRIVATE-KEEP").write_text("keep", encoding="utf-8")
    identity = module._ownership_identity(module._require_directory(staging))

    assert module._cleanup_owned_staging(tmp_path, staging, identity) is True

    if entry_kind == "file":
        assert (staging / "owned").read_text(encoding="utf-8") == "owned"
    else:
        assert (staging / "owned" / "payload").read_text(
            encoding="utf-8"
        ) == "owned"
    assert (unrelated / "PRIVATE-KEEP").read_text(encoding="utf-8") == "keep"


def test_cleanup_never_overwrites_unrelated_retained_sentinel(
    tmp_path: Path,
) -> None:
    import xrag.translation_model as module

    temporary = tmp_path / f".manifest-{'8' * 32}.tmp"
    temporary.write_text("owned", encoding="utf-8")
    identity = module._ownership_identity(module._require_regular_file(temporary))
    claimed = tmp_path / f".retained-{'8' * 32}"
    claimed.write_text("PRIVATE-KEEP", encoding="utf-8")

    assert module._cleanup_owned_manifest(tmp_path, temporary, identity) is True

    assert claimed.read_text(encoding="utf-8") == "PRIVATE-KEEP"
    assert temporary.read_text(encoding="utf-8") == "owned"


@pytest.mark.skipif(os.name == "nt", reason="POSIX final-name swap probe")
def test_posix_file_final_name_swap_never_deletes_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'5' * 32}"
    staging.mkdir()
    (staging / "owned").write_text("owned", encoding="utf-8")
    unowned = tmp_path / "PRIVATE-KEEP-file"
    unowned.write_text("keep", encoding="utf-8")
    parked = tmp_path / "parked-final-file"
    identity = module._ownership_identity(module._require_directory(staging))
    real_unlink = module.os.unlink
    attacked = False

    def swapping_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal attacked
        parent_descriptor = kwargs.get("dir_fd")
        if (
            isinstance(path, str)
            and path.startswith(".quarantine-")
            and isinstance(parent_descriptor, int)
            and not attacked
        ):
            parent = Path(os.readlink(f"/proc/self/fd/{parent_descriptor}"))
            candidate = parent / path
            if candidate.is_file():
                attacked = True
                os.replace(candidate, parked)
                os.replace(unowned, candidate)
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "unlink", swapping_unlink)

    module._cleanup_owned_staging(tmp_path, staging, identity)

    assert unowned.read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name == "nt", reason="POSIX final-name swap probe")
def test_posix_child_directory_final_name_swap_never_deletes_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'6' * 32}"
    (staging / "owned-directory").mkdir(parents=True)
    unowned = tmp_path / "PRIVATE-KEEP-directory"
    unowned.mkdir()
    parked = tmp_path / "parked-final-directory"
    identity = module._ownership_identity(module._require_directory(staging))
    real_rmdir = module.os.rmdir
    attacked = False

    def swapping_rmdir(path: object, *args: object, **kwargs: object) -> None:
        nonlocal attacked
        parent_descriptor = kwargs.get("dir_fd")
        if (
            isinstance(path, str)
            and path.startswith(".quarantine-")
            and isinstance(parent_descriptor, int)
            and not attacked
        ):
            parent = Path(os.readlink(f"/proc/self/fd/{parent_descriptor}"))
            if parent != tmp_path.resolve():
                attacked = True
                candidate = parent / path
                os.replace(candidate, parked)
                os.replace(unowned, candidate)
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "rmdir", swapping_rmdir)

    module._cleanup_owned_staging(tmp_path, staging, identity)

    assert unowned.is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX final-name swap probe")
def test_posix_root_final_name_swap_never_deletes_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    staging = tmp_path / f".install-{'7' * 32}"
    staging.mkdir()
    unowned = tmp_path / "PRIVATE-KEEP-root"
    unowned.mkdir()
    parked = tmp_path / "parked-final-root"
    identity = module._ownership_identity(module._require_directory(staging))
    real_rmdir = module.os.rmdir
    attacked = False

    def swapping_rmdir(path: object, *args: object, **kwargs: object) -> None:
        nonlocal attacked
        parent_descriptor = kwargs.get("dir_fd")
        if (
            isinstance(path, str)
            and path.startswith(".quarantine-")
            and isinstance(parent_descriptor, int)
            and not attacked
        ):
            parent = Path(os.readlink(f"/proc/self/fd/{parent_descriptor}"))
            if parent == tmp_path.resolve():
                attacked = True
                candidate = parent / path
                os.replace(candidate, parked)
                os.replace(unowned, candidate)
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "rmdir", swapping_rmdir)

    module._cleanup_owned_staging(tmp_path, staging, identity)

    assert unowned.is_dir()


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decoded: object = None,
    load_error: Exception | None = None,
    on_model_load: Callable[[], None] | None = None,
) -> SimpleNamespace:
    state = SimpleNamespace(
        tokenizer_loads=[],
        model_loads=[],
        tokenizer_calls=[],
        generate_calls=[],
        device_calls=[],
        eval_calls=0,
        decoded=[" 翻译一 ", "翻译二"] if decoded is None else decoded,
    )

    class Tokenizer:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: object) -> object:
            state.tokenizer_loads.append((path, kwargs))
            if load_error is not None:
                raise load_error
            return cls()

        def __call__(self, texts: list[str], **kwargs: object) -> dict[str, object]:
            state.tokenizer_calls.append((texts, kwargs))
            return {"input_ids": "encoded", "attention_mask": "mask"}

        def batch_decode(self, _tokens: object, **kwargs: object) -> object:
            state.decode_kwargs = kwargs
            return state.decoded

    class Model:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: object) -> object:
            state.model_loads.append((path, kwargs))
            if on_model_load is not None:
                on_model_load()
            return cls()

        def to(self, device: str) -> None:
            state.device_calls.append(device)

        def eval(self) -> None:
            state.eval_calls += 1

        def generate(self, **kwargs: object) -> object:
            state.generate_calls.append(kwargs)
            return "generated"

    transformers = ModuleType("transformers")
    transformers.AutoTokenizer = Tokenizer  # type: ignore[attr-defined]
    transformers.AutoModelForSeq2SeqLM = Model  # type: ignore[attr-defined]
    torch = ModuleType("torch")
    torch.inference_mode = nullcontext  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setitem(sys.modules, "torch", torch)
    return state


def test_engine_loads_verified_snapshot_offline_on_cpu_and_translates_deterministically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(tmp_path)
    state = _install_fake_runtime(monkeypatch)
    engine = TransformersTranslationEngine(tmp_path)

    assert engine.model_id == MODEL_ID
    assert engine.revision == REVISION_A
    result = engine.translate_many(["first", "second"])

    snapshot = str((tmp_path / "snapshots" / REVISION_A).resolve())
    expected_load = (snapshot, {"local_files_only": True, "trust_remote_code": False})
    assert state.tokenizer_loads == [expected_load]
    assert state.model_loads == [expected_load]
    assert state.device_calls == ["cpu"]
    assert state.eval_calls == 1
    assert state.tokenizer_calls == [
        (
            ["first", "second"],
            {
                "return_tensors": "pt",
                "padding": True,
                "truncation": True,
                "max_length": 512,
            },
        )
    ]
    assert state.generate_calls == [
        {
            "input_ids": "encoded",
            "attention_mask": "mask",
            "do_sample": False,
            "num_beams": 4,
            "max_new_tokens": 64,
        }
    ]
    assert state.decode_kwargs == {"skip_special_tokens": True}
    assert result == ["翻译一", "翻译二"]


def test_engine_caps_generation_for_long_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(tmp_path)
    state = _install_fake_runtime(monkeypatch, decoded=["translation"])

    TransformersTranslationEngine(tmp_path).translate_many(["word " * 200])

    assert state.generate_calls[0]["max_new_tokens"] == 256


def test_engine_empty_batch_does_not_preflight_or_import_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    monkeypatch.setattr(
        module,
        "verify_translation_model",
        lambda _root: pytest.fail("empty batch must not preflight"),
    )
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.delitem(sys.modules, "transformers", raising=False)

    assert TransformersTranslationEngine(tmp_path).translate_many([]) == []


@pytest.mark.parametrize("texts", ["plain string", b"bytes", [""], ["  "], [123]])
def test_engine_rejects_invalid_input_with_generic_error(
    tmp_path: Path, texts: object
) -> None:
    with pytest.raises(RuntimeError, match="^local translation failed$"):
        TransformersTranslationEngine(tmp_path).translate_many(texts)  # type: ignore[arg-type]


@pytest.mark.parametrize("decoded", ["not-a-list", [], ["one"], ["one", " "], ["one", 2]])
def test_engine_rejects_invalid_model_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decoded: object
) -> None:
    _write_snapshot(tmp_path)
    _install_fake_runtime(monkeypatch, decoded=decoded)

    with pytest.raises(RuntimeError, match="^local translation failed$"):
        TransformersTranslationEngine(tmp_path).translate_many(["one", "two"])


def test_engine_redacts_dependency_errors_and_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(tmp_path)
    _install_fake_runtime(
        monkeypatch, load_error=OSError("SECRET dependency detail")
    )
    secret_input = "PRIVATE INPUT"

    with pytest.raises(RuntimeError) as caught:
        TransformersTranslationEngine(tmp_path).translate_many([secret_input])

    assert str(caught.value) == "local translation failed"
    assert "SECRET" not in str(caught.value)
    assert secret_input not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_engine_preflight_caches_only_unchanged_verified_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path)
    real_verify = module.verify_translation_model
    calls = 0

    def counting_verify(root: Path) -> InstalledTranslationModel:
        nonlocal calls
        calls += 1
        return real_verify(root)

    monkeypatch.setattr(module, "verify_translation_model", counting_verify)
    engine = TransformersTranslationEngine(tmp_path)
    engine.preflight()
    engine.preflight()
    assert calls == 1

    config = tmp_path / "snapshots" / REVISION_A / "config.json"
    config.write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        engine.preflight()
    assert calls == 2


def test_engine_force_verifies_again_immediately_before_first_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, files={"config.json": b"good"})
    real_verify = module.verify_translation_model
    calls = 0

    def counting_verify(root: Path) -> InstalledTranslationModel:
        nonlocal calls
        calls += 1
        return real_verify(root)

    monkeypatch.setattr(module, "verify_translation_model", counting_verify)
    engine = TransformersTranslationEngine(tmp_path)
    engine.preflight()
    config = tmp_path / "snapshots" / REVISION_A / "config.json"
    original = config.stat()
    config.write_bytes(b"evil")
    os.utime(config, ns=(original.st_atime_ns, original.st_mtime_ns))
    _install_fake_runtime(monkeypatch, decoded=["unused"])

    with pytest.raises(RuntimeError, match="^local translation failed$"):
        engine.translate_many(["first"])

    assert calls == 2


def test_engine_fingerprint_tracks_empty_directories_and_rejects_new_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path)
    empty = tmp_path / "snapshots" / REVISION_A / "empty"
    empty.mkdir()
    real_verify = module.verify_translation_model
    calls = 0

    def counting_verify(root: Path) -> InstalledTranslationModel:
        nonlocal calls
        calls += 1
        return real_verify(root)

    monkeypatch.setattr(module, "verify_translation_model", counting_verify)
    engine = TransformersTranslationEngine(tmp_path)
    engine.preflight()
    (empty / "PRIVATE-extra.bin").write_bytes(b"extra")

    with pytest.raises(RuntimeError, match="^local translation model unavailable$"):
        engine.preflight()

    assert calls == 2


def test_engine_loaded_model_uses_memory_when_disk_metadata_appears_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xrag.translation_model as module

    _write_snapshot(tmp_path, files={"config.json": b"good"})
    config = tmp_path / "snapshots" / REVISION_A / "config.json"
    snapshot = config.parent

    def touch_loaded_metadata() -> None:
        metadata = config.stat()
        os.utime(
            config,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
        )

    state = _install_fake_runtime(
        monkeypatch,
        decoded=["memory result"],
        on_model_load=touch_loaded_metadata,
    )
    engine = TransformersTranslationEngine(tmp_path)
    assert engine.translate_many(["first"]) == ["memory result"]

    original = os.lstat(config)
    original_snapshot = os.lstat(snapshot)
    config.write_bytes(b"evil")
    os.utime(config, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.utime(
        snapshot,
        ns=(original_snapshot.st_atime_ns, original_snapshot.st_mtime_ns),
    )
    assert engine._fingerprint == module._model_fingerprint(engine._installed)
    monkeypatch.setattr(
        module,
        "_hash_file",
        lambda _path: pytest.fail("loaded model must not reread unchanged metadata"),
    )
    state.decoded = ["still memory"]

    assert engine.translate_many(["second"]) == ["still memory"]
    assert len(state.model_loads) == 1


def test_engine_reloads_when_verified_snapshot_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_snapshot(tmp_path, REVISION_A, {"config.json": b"old"})
    state = _install_fake_runtime(monkeypatch, decoded=["first"])
    engine = TransformersTranslationEngine(tmp_path)
    assert engine.translate_many(["one"]) == ["first"]

    _write_snapshot(tmp_path, REVISION_B, {"config.json": b"new"})
    state.decoded = ["second"]
    assert engine.translate_many(["two"]) == ["second"]

    assert len(state.tokenizer_loads) == 2
    assert state.tokenizer_loads[0][0].endswith(REVISION_A)
    assert state.tokenizer_loads[1][0].endswith(REVISION_B)
