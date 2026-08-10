from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.config import load_config


APPROVED_KEYWORDS = (
    '"Autonomous AI Agents" OR 自主智能体 OR "Rogue AI Agents" OR "Agent Security" OR "AI Safety Evaluation" OR "AI Cybersecurity"',
    '"World Models" OR 世界模型 OR "Open-weight Models" OR AGI OR "Intelligence Explosion" OR "Embodied AI" OR 具身智能 OR "Humanoid Robots"',
    'RWA OR 现实资产代币化 OR "Tokenized Stocks" OR "Stablecoin Payments" OR "Solana RWA"',
    '"Prediction Markets" OR "AI Agents Crypto" OR x402 OR "On-chain Perps" OR "Crypto ETF" OR MiCA OR "CLARITY Act" OR 加密监管',
)


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
    assert config.media_dir == tmp_path / "data" / "media"
    assert config.dashboard_dir == tmp_path / "data" / "dashboard-site"
    assert config.dashboard_source_dir == tmp_path / "dashboard"
    assert config.pages_worktree == tmp_path / ".worktrees" / "x-rag-pages"


def test_repository_config_uses_four_approved_daily_topic_groups() -> None:
    root = Path(__file__).resolve().parents[1]

    config = load_config(root)

    assert config.schedule_time == "10:00"
    assert config.timezone == "Asia/Singapore"
    assert config.limit_per_keyword == 10
    assert config.keywords == APPROVED_KEYWORDS


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
