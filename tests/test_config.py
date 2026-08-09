from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import load_config


def write_config(
    root: Path,
    schedule_time: str,
    keywords: list[str],
    schedule_enabled: str = "true",
    limit_per_keyword: str = "50",
    delay_seconds: str = "10",
) -> None:
    config_dir = root / "config"
    config_dir.mkdir()
    config_dir.joinpath("keywords.yaml").write_text(
        f"""schedule:
  enabled: {schedule_enabled}
  time: \"{schedule_time}\"
  timezone: Asia/Singapore
collection:
  limit_per_keyword: {limit_per_keyword}
  delay_seconds: {delay_seconds}
keywords:
{''.join(f'  - {keyword!r}\n' for keyword in keywords)}embedding:
  model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
""",
        encoding="utf-8",
    )


def test_load_config_reads_valid_configuration(tmp_path: Path) -> None:
    write_config(tmp_path, "10:00", ["人工智能", "AI 视频", "人工智能", "  "])

    config = load_config(tmp_path)

    assert config.schedule_time == "10:00"
    assert config.keywords == ("人工智能", "AI 视频")
    assert config.markdown_dir == tmp_path / "data" / "markdown"


def test_load_config_rejects_invalid_schedule_time(tmp_path: Path) -> None:
    write_config(tmp_path, "25:00", ["人工智能"])

    with pytest.raises(ValueError, match="HH:MM"):
        load_config(tmp_path)


@pytest.mark.parametrize("schedule_time", ["1:00", "01:0", "1:0"])
def test_load_config_rejects_non_zero_padded_schedule_time(
    tmp_path: Path, schedule_time: str
) -> None:
    write_config(tmp_path, schedule_time, ["人工智能"])

    with pytest.raises(ValueError, match="HH:MM"):
        load_config(tmp_path)


@pytest.mark.parametrize(
    ("config_field", "value", "error"),
    [
        ("schedule_enabled", '"false"', "schedule.enabled.*boolean"),
        ("limit_per_keyword", "true", "collection.limit_per_keyword"),
        ("limit_per_keyword", "0", "collection.limit_per_keyword"),
        ("limit_per_keyword", "-1", "collection.limit_per_keyword"),
        ("delay_seconds", "-1", "collection.delay_seconds"),
    ],
)
def test_load_config_rejects_invalid_collection_values(
    tmp_path: Path, config_field: str, value: str, error: str
) -> None:
    write_config(tmp_path, "10:00", ["人工智能"], **{config_field: value})

    with pytest.raises(ValueError, match=error):
        load_config(tmp_path)


def test_load_config_rejects_empty_keyword_list(tmp_path: Path) -> None:
    write_config(tmp_path, "10:00", ["", "  "])

    with pytest.raises(ValueError, match="keyword"):
        load_config(tmp_path)
