from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

import yaml


@dataclass(frozen=True)
class AppConfig:
    root: Path
    schedule_enabled: bool
    schedule_time: str
    timezone: str
    limit_per_keyword: int
    delay_seconds: int
    keywords: tuple[str, ...]
    embedding_model: str

    @property
    def markdown_dir(self) -> Path:
        return self.root / "data" / "markdown"

    @property
    def import_dir(self) -> Path:
        return self.root / "data" / "imports"

    @property
    def chroma_dir(self) -> Path:
        return self.root / "data" / "chroma"

    @property
    def log_dir(self) -> Path:
        return self.root / "logs"


def load_config(root: Path) -> AppConfig:
    resolved_root = root.resolve()
    config_path = resolved_root / "config" / "keywords.yaml"
    with config_path.open(encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)

    if not isinstance(raw_config, dict):
        raise ValueError("Configuration must be a mapping")

    schedule = _mapping(raw_config, "schedule")
    collection = _mapping(raw_config, "collection")
    embedding = _mapping(raw_config, "embedding")
    schedule_time = _string(schedule, "time")
    if not re.fullmatch(r"\d{2}:\d{2}", schedule_time):
        raise ValueError("Schedule time must use HH:MM format")
    try:
        datetime.strptime(schedule_time, "%H:%M")
    except ValueError as error:
        raise ValueError("Schedule time must use HH:MM format") from error

    keywords = _keywords(raw_config.get("keywords"))
    return AppConfig(
        root=resolved_root,
        schedule_enabled=_boolean(schedule, "enabled", "schedule.enabled"),
        schedule_time=schedule_time,
        timezone=_string(schedule, "timezone"),
        limit_per_keyword=_positive_int(
            collection, "limit_per_keyword", "collection.limit_per_keyword"
        ),
        delay_seconds=_non_negative_int(
            collection, "delay_seconds", "collection.delay_seconds"
        ),
        keywords=keywords,
        embedding_model=_string(embedding, "model"),
    )


def _mapping(config: dict[object, object], name: str) -> dict[object, object]:
    value = config.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"Configuration section {name!r} must be a mapping")
    return value


def _string(config: dict[object, object], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str):
        raise ValueError(f"Configuration value {name!r} must be a string")
    return value


def _boolean(config: dict[object, object], name: str, field: str) -> bool:
    value = config.get(name)
    if type(value) is not bool:
        raise ValueError(f"Configuration value {field!r} must be a boolean")
    return value


def _positive_int(config: dict[object, object], name: str, field: str) -> int:
    value = config.get(name)
    if type(value) is not int or value <= 0:
        raise ValueError(f"Configuration value {field!r} must be a positive integer")
    return value


def _non_negative_int(config: dict[object, object], name: str, field: str) -> int:
    value = config.get(name)
    if type(value) is not int or value < 0:
        raise ValueError(
            f"Configuration value {field!r} must be a non-negative integer"
        )
    return value


def _keywords(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Keywords must be a list")

    keywords: list[str] = []
    for keyword in value:
        if not isinstance(keyword, str):
            raise ValueError("Keywords must contain strings")
        normalized = keyword.strip()
        if normalized and normalized not in keywords:
            keywords.append(normalized)

    if not keywords:
        raise ValueError("At least one keyword is required")
    return tuple(keywords)
