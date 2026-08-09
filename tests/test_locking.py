from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def test_writer_lock_releases_and_reports_contention(tmp_path: Path) -> None:
    pytest.importorskip("portalocker")
    from xrag.locking import writer_lock

    with writer_lock(tmp_path, timeout=0.05):
        assert (tmp_path / ".xrag.lock").exists()
        with pytest.raises(Exception, match="lock|Lock|timed out|timeout"):
            with writer_lock(tmp_path, timeout=0.05):
                pass

    with writer_lock(tmp_path, timeout=0.05):
        pass


def test_writer_lock_does_not_translate_exceptions_from_the_protected_body(tmp_path: Path) -> None:
    portalocker = pytest.importorskip("portalocker")
    from xrag.locking import writer_lock

    failure = portalocker.exceptions.LockException("raised by body")
    with pytest.raises(portalocker.exceptions.LockException) as caught:
        with writer_lock(tmp_path):
            raise failure

    assert caught.value is failure
