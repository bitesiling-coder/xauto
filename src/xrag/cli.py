from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Annotated, Any, Callable, TypeVar

import typer
import yaml

from .config import load_config
from .dashboard_export import DashboardBuilder
from .dashboard_publish import PagesPublisher
from .markdown_store import MarkdownStore
from .media_store import MediaStore
from .opencli import OpenCLIClient, OpenCLIError
from .service import XragService
from .vector_store import VectorStore


app = typer.Typer(no_args_is_help=True)
dashboard_app = typer.Typer(no_args_is_help=True)
app.add_typer(dashboard_app, name="dashboard")
T = TypeVar("T")

_SECRET = re.compile(
    r"(?i)[\"']?\b((?:(?:twitter|x)[_-])?(?:auth[_-]?token|ct0|api[_-]?key|"
    r"password|passwd|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
    r"authorization))\b[\"']?\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_AUTHORIZATION = re.compile(
    r'''(?ix)
    ["']?\b(?:(?:twitter|x)[_-])?authorization\b["']?\s*[:=]\s*
    [^\r\n]*
    '''
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;}\]]+")


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option("--root", help="Project root containing config/keywords.yaml."),
    ] = Path("."),
) -> None:
    _configure_utf8_streams()
    ctx.obj = root.resolve()


def build_service(root: Path) -> XragService:
    config = load_config(root.resolve())
    markdown = MarkdownStore(config.markdown_dir)
    media = MediaStore(config.media_dir)

    def vector_factory(path: Path) -> VectorStore:
        return VectorStore.persistent(path, config.embedding_model)

    return XragService(
        config,
        OpenCLIClient(),
        markdown,
        None,
        media=media,
        vector_factory=vector_factory,
        rebuild_factory=vector_factory,
    )


def build_rebuild_service(root: Path) -> XragService:
    return build_service(root)


def build_dashboard(root: Path) -> DashboardBuilder:
    config = load_config(root.resolve())
    return DashboardBuilder(config, MarkdownStore(config.markdown_dir))


def build_pages_publisher(root: Path) -> PagesPublisher:
    config = load_config(root.resolve())
    return PagesPublisher(config.root, config.pages_worktree)


def _service(ctx: typer.Context) -> XragService:
    return build_service(ctx.obj)


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (OpenCLIError, yaml.YAMLError, ValueError, RuntimeError, OSError) as error:
        typer.echo(f"Error: {_redact(str(error))}", err=True)
        raise typer.Exit(code=2) from None


def _redact(message: str) -> str:
    message = _AUTHORIZATION.sub("authorization=[REDACTED]", message)
    message = _BEARER.sub("Bearer [REDACTED]", message)
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)


def _configure_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _summary(keyword: str, counts: dict[str, int]) -> str:
    return (
        f"{keyword}: found={counts['found']} stored={counts['stored']} "
        f"chunks={counts['chunks']} errors={counts['errors']}"
    )


@app.command()
def collect(
    ctx: typer.Context,
    keyword: Annotated[str | None, typer.Argument(help="Keyword to collect.")] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Maximum posts for keyword mode."),
    ] = None,
    all_keywords: Annotated[
        bool,
        typer.Option("--all", help="Collect every configured keyword."),
    ] = False,
) -> None:
    if (keyword is None) == (not all_keywords):
        typer.echo("Error: exactly one of KEYWORD or --all is required.", err=True)
        raise typer.Exit(code=2)
    if all_keywords and limit is not None:
        typer.echo("Error: --limit cannot be used with --all.", err=True)
        raise typer.Exit(code=2)
    if all_keywords:
        results = _run(lambda: _service(ctx).collect_all())
        for item_keyword, counts in results:
            typer.echo(_summary(item_keyword, counts))
        return
    assert keyword is not None
    counts = _run(lambda: _service(ctx).collect(keyword, limit))
    typer.echo(_summary(keyword, counts))


@app.command("import")
def import_command(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="File or directory to import.")],
) -> None:
    result = _run(lambda: _service(ctx).import_path(source))
    _print_json(result)


@app.command()
def search(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Semantic search query.")],
    top: Annotated[
        int,
        typer.Option("--top", min=1, max=100, help="Maximum results."),
    ] = 10,
) -> None:
    hits = _run(lambda: _service(ctx).search(query, top))
    if not hits:
        typer.echo("No results found.")
        return
    for rank, hit in enumerate(hits, start=1):
        typer.echo(f"{rank}. [{hit.score:.4f}] @{hit.author} · {hit.created_at}")
        typer.echo(hit.text)
        typer.echo(f"URL: {hit.url}")
        typer.echo(f"Markdown: {hit.markdown_path}")


@app.command()
def status(ctx: typer.Context) -> None:
    _print_json(_run(lambda: _service(ctx).status()))


@app.command()
def rebuild(ctx: typer.Context) -> None:
    _print_json(_run(lambda: build_rebuild_service(ctx.obj).rebuild()))


@dashboard_app.command("build")
def dashboard_build(ctx: typer.Context) -> None:
    _print_json(_run(lambda: build_dashboard(ctx.obj).build()))


def _build_and_publish(root: Path) -> dict[str, object]:
    build_result = build_dashboard(root).build()
    site_dir = root.resolve() / "data" / "dashboard-site"
    publish_result = build_pages_publisher(root).publish(site_dir)
    return {"build": build_result, "publish": publish_result}


@dashboard_app.command("publish")
def dashboard_publish(ctx: typer.Context) -> None:
    _print_json(_run(lambda: _build_and_publish(ctx.obj)))


def _collect_build_publish(
    root: Path, *, publish: bool = True
) -> dict[str, object]:
    collection = build_service(root).collect_all()
    if sum(counts["stored"] for _, counts in collection) == 0:
        raise RuntimeError(
            "Collection stored no posts; dashboard publication stopped"
        )
    build_result = build_dashboard(root).build()
    result: dict[str, object] = {
        "collection": collection,
        "build": build_result,
    }
    if publish:
        site_dir = root.resolve() / "data" / "dashboard-site"
        result["publish"] = build_pages_publisher(root).publish(site_dir)
    return result


@dashboard_app.command("update")
def dashboard_update(
    ctx: typer.Context,
    publish: Annotated[
        bool,
        typer.Option(
            "--publish/--no-publish",
            help="Publish with Git after collection and dashboard build.",
        ),
    ] = True,
) -> None:
    _print_json(
        _run(lambda: _collect_build_publish(ctx.obj, publish=publish))
    )


def _print_json(value: Any) -> None:
    typer.echo(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default)
    )


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    _configure_utf8_streams()
    app()
