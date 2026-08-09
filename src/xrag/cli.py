from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Annotated, Any, Callable, TypeVar

import typer

from .config import load_config
from .markdown_store import MarkdownStore
from .opencli import OpenCLIClient, OpenCLIError
from .service import XragService
from .vector_store import VectorStore


app = typer.Typer(no_args_is_help=True)
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
    (?:"[^"]*"|'[^']*'|(?:Basic|Bearer|Token)\s+[^\s,;}]+|
       Digest\b[^\r\n;]*|[^\r\n,;]*)
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
    ctx.obj = root.resolve()


def build_service(root: Path) -> XragService:
    config = load_config(root.resolve())
    markdown = MarkdownStore(config.markdown_dir)
    vectors = VectorStore.persistent(config.chroma_dir, config.embedding_model)
    return XragService(config, OpenCLIClient(), markdown, vectors)


def _service(ctx: typer.Context) -> XragService:
    return build_service(ctx.obj)


def _run(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except (OpenCLIError, ValueError, RuntimeError, OSError) as error:
        typer.echo(f"Error: {_redact(str(error))}", err=True)
        raise typer.Exit(code=2) from None


def _redact(message: str) -> str:
    message = _AUTHORIZATION.sub("authorization=[REDACTED]", message)
    message = _BEARER.sub("Bearer [REDACTED]", message)
    return _SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)


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
    _print_json(_run(lambda: _service(ctx).rebuild()))


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
