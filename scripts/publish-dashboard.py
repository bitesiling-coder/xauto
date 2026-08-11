from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish a prepared X-RAG dashboard with Windows Git."
    )
    parser.add_argument("--root", required=True)
    return parser


def _publish(root_value: str) -> dict[str, object]:
    root = Path(root_value).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("invalid project root")
    source = root / "src"
    if not source.is_dir():
        raise ValueError("invalid project root")
    sys.path.insert(0, str(source))
    try:
        from xrag.dashboard_publish import PagesPublisher

        publisher = PagesPublisher(root, root / ".worktrees" / "x-rag-pages")
        return publisher.publish(root / "data" / "dashboard-site")
    finally:
        sys.path.remove(str(source))


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        result = _publish(arguments.root)
    except (ImportError, OSError, RuntimeError, ValueError):
        print("Error: dashboard publication failed", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
