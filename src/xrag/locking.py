from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def writer_lock(root: Path, timeout: float = 1) -> Iterator[None]:
    """Hold the single-writer lock for an X-RAG project root."""
    import portalocker

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".xrag.lock"
    lock = portalocker.Lock(lock_path, mode="a", timeout=timeout)
    try:
        lock.acquire()
    except portalocker.exceptions.LockException as error:
        raise RuntimeError(
            f"Could not acquire X-RAG writer lock {lock_path} within {timeout} seconds"
        ) from error
    try:
        yield
    finally:
        lock.release()
