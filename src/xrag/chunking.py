from __future__ import annotations

import re


def chunk_text(text: str, max_chars: int = 500, overlap: int = 80) -> list[str]:
    """Split text into paragraph-aware, character-sized chunks."""
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be at least zero and less than max_chars")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", normalized)]
    step = max_chars - overlap
    chunks: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            chunks.append(paragraph)
            continue
        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + max_chars])
            if start + max_chars >= len(paragraph):
                break
            start += step
    return chunks
