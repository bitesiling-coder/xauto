import pytest

from xrag.chunking import chunk_text


def test_keeps_short_paragraphs_intact():
    assert chunk_text("第一段。\n\n第二段。", max_chars=20, overlap=3) == ["第一段。", "第二段。"]


def test_splits_long_paragraphs_with_character_overlap():
    assert chunk_text("甲乙丙丁戊己庚辛壬癸", max_chars=6, overlap=2) == [
        "甲乙丙丁戊己",
        "戊己庚辛壬癸",
    ]


def test_normalizes_crlf_and_skips_empty_paragraphs():
    assert chunk_text("\r\n  第一段  \r\n\r\n\r\n第二段\r\n") == ["第一段", "第二段"]
    assert chunk_text(" \r\n\r\n ") == []


@pytest.mark.parametrize(
    ("max_chars", "overlap"),
    [(0, 0), (-1, 0), (5, -1), (5, 5), (5, 6)],
)
def test_rejects_invalid_chunk_sizes(max_chars, overlap):
    with pytest.raises(ValueError):
        chunk_text("内容", max_chars=max_chars, overlap=overlap)
