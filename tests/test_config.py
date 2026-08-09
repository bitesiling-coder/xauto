from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import load_config


def write_config(root: Path, schedule_time: str, keywords: list[str]) -> None:
    config_dir = root / "config"
    config_dir.mkdir()
    config_dir.joinpath("keywords.yaml").write_text(
        f"""schedule:
  enabled: true
  time: \"{schedule_time}\"
  timezone: Asia/Singapore
collection:
  limit_per_keyword: 50
  delay_seconds: 10
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


def test_load_config_rejects_empty_keyword_list(tmp_path: Path) -> None:
    write_config(tmp_path, "10:00", ["", "  "])

    with pytest.raises(ValueError, match="keyword"):
        load_config(tmp_path)
