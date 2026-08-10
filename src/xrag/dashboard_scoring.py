from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import re
from typing import Callable, Iterable, Literal, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Post


@dataclass(frozen=True)
class Topic:
    id: str
    label: str
    family: Literal["AI", "Web3"]


@dataclass(frozen=True)
class RankedPost:
    post: Post
    topic: Topic
    score: float
    engagement: float
    freshness: float
    topic_frequency: float
    completeness: float
    fallback: bool


TOPICS = (
    Topic("ai-agents-security", "AI Agents 与 Agent Security", "AI"),
    Topic("world-models-embodied-ai", "World Models 与 Embodied AI", "AI"),
    Topic("rwa-stablecoin-payments", "RWA 与 Stablecoin Payments", "Web3"),
    Topic(
        "prediction-markets-regulation",
        "Prediction Markets 与 Crypto Regulation",
        "Web3",
    ),
)

_X_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<weekday>Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
    r"(?P<month>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"(?P<day>0[1-9]|[12][0-9]|3[01]) "
    r"(?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9]):(?P<second>[0-5][0-9]) "
    r"(?P<offset_sign>[+-])(?P<offset_hour>[01][0-9]|2[0-3])"
    r"(?P<offset_minute>[0-5][0-9]) (?P<year>[0-9]{4})$"
)
_X_MONTHS = {
    month: index
    for index, month in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        start=1,
    )
}
_X_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class _PreparedPost:
    post: Post
    topic: Topic
    published_at: datetime
    is_today: bool


def rank_posts(
    posts: Iterable[Post],
    *,
    now: datetime,
    timezone_name: str,
    configured_keywords: Sequence[str],
    minimum_today: int = 6,
    window_hours: int = 48,
    limit: int = 12,
    max_per_author: int = 3,
) -> list[RankedPost]:
    _validate_clock(now)
    _validate_positive_limits(
        minimum_today=minimum_today,
        window_hours=window_hours,
        limit=limit,
        max_per_author=max_per_author,
    )
    try:
        local_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError) as error:
        raise ValueError(f"Unknown timezone: {timezone_name!r}") from error

    local_now = now.astimezone(local_timezone)
    keywords = tuple(configured_keywords)
    prepared: list[_PreparedPost] = []
    for post in posts:
        topic = _assign_topic(post, keywords)
        published_at = _parse_timestamp(post.created_at)
        if topic is None or published_at is None:
            continue
        if published_at - now > timedelta(minutes=5):
            continue
        prepared.append(
            _PreparedPost(
                post=post,
                topic=topic,
                published_at=published_at,
                is_today=published_at.astimezone(local_timezone).date()
                == local_now.date(),
            )
        )
    prepared = _deduplicate(prepared)

    today_count = sum(item.is_today for item in prepared)
    if today_count >= minimum_today:
        candidates = [item for item in prepared if item.is_today]
    else:
        window_start = now - timedelta(hours=window_hours)
        candidates = [item for item in prepared if item.published_at >= window_start]

    if not candidates:
        return []

    maximum_views = max(_metric(item.post.views) for item in candidates)
    maximum_likes = max(_metric(item.post.likes) for item in candidates)
    topic_counts: dict[Topic, int] = {}
    for item in candidates:
        topic_counts[item.topic] = topic_counts.get(item.topic, 0) + 1
    largest_topic_count = max(topic_counts.values())

    ranked: list[tuple[RankedPost, datetime]] = []
    for item in candidates:
        engagement = _engagement(item.post, maximum_views, maximum_likes)
        age_hours = max(0.0, (now - item.published_at).total_seconds() / 3600)
        freshness = max(0.0, 1.0 - age_hours / window_hours)
        topic_frequency = topic_counts[item.topic] / largest_topic_count
        completeness = _completeness(item.post)
        score = (
            0.40 * engagement
            + 0.30 * freshness
            + 0.20 * topic_frequency
            + 0.10 * completeness
        )
        ranked.append(
            (
                RankedPost(
                    post=item.post,
                    topic=item.topic,
                    score=score,
                    engagement=engagement,
                    freshness=freshness,
                    topic_frequency=topic_frequency,
                    completeness=completeness,
                    fallback=not item.is_today,
                ),
                item.published_at,
            )
        )

    ranked.sort(
        key=lambda item: (
            -item[0].score,
            -item[1].timestamp(),
            item[0].post.id,
        )
    )
    selected: list[RankedPost] = []
    author_counts: dict[str, int] = {}
    for item, _ in ranked:
        author_key = item.post.author.strip().casefold() or "unknown"
        if author_counts.get(author_key, 0) >= max_per_author:
            continue
        author_counts[author_key] = author_counts.get(author_key, 0) + 1
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _validate_clock(now: datetime) -> None:
    try:
        offset = now.utcoffset()
    except (AttributeError, ValueError) as error:
        raise ValueError("now must be timezone-aware") from error
    if now.tzinfo is None or offset is None:
        raise ValueError("now must be timezone-aware")


def _validate_positive_limits(**limits: int) -> None:
    for name, value in limits.items():
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")


def _parse_timestamp(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except (ValueError, TypeError, OverflowError):
        match = _X_TIMESTAMP_PATTERN.fullmatch(normalized)
        if match is None:
            return None
        parts = match.groupdict()
        offset_minutes = int(parts["offset_hour"]) * 60 + int(
            parts["offset_minute"]
        )
        if parts["offset_sign"] == "-":
            offset_minutes = -offset_minutes
        try:
            parsed = datetime(
                int(parts["year"]),
                _X_MONTHS[parts["month"]],
                int(parts["day"]),
                int(parts["hour"]),
                int(parts["minute"]),
                int(parts["second"]),
                tzinfo=timezone(timedelta(minutes=offset_minutes)),
            )
        except (ValueError, OverflowError):
            return None
        if _X_WEEKDAYS[parsed.weekday()] != parts["weekday"]:
            return None
        offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None:
        return None
    return parsed


def _deduplicate(posts: Iterable[_PreparedPost]) -> list[_PreparedPost]:
    by_id = _retain_best(posts, lambda item: item.post.id.strip().casefold())
    return _retain_best(by_id, lambda item: item.post.url.strip().casefold())


def _retain_best(
    posts: Iterable[_PreparedPost],
    key_function: Callable[[_PreparedPost], str],
) -> list[_PreparedPost]:
    keyed: dict[str, tuple[int, _PreparedPost]] = {}
    unkeyed: list[tuple[int, _PreparedPost]] = []
    for index, item in enumerate(posts):
        key = key_function(item)
        if not key:
            unkeyed.append((index, item))
            continue
        current = keyed.get(key)
        if current is None:
            keyed[key] = (index, item)
        elif _dedupe_quality(item.post) > _dedupe_quality(current[1].post):
            keyed[key] = (current[0], item)
    retained = [*keyed.values(), *unkeyed]
    retained.sort(key=lambda item: item[0])
    return [item for _, item in retained]


def _dedupe_quality(post: Post) -> tuple[float, int, int, int]:
    return (
        _completeness(post),
        _metric(post.views),
        _metric(post.likes),
        len(post.text),
    )


def _assign_topic(post: Post, configured_keywords: Sequence[str]) -> Topic | None:
    sourced = set(post.source_keywords)
    possible = [
        (index, keyword)
        for index, keyword in enumerate(configured_keywords[: len(TOPICS)])
        if keyword in sourced
    ]
    if not possible:
        return None
    text = post.searchable_text.casefold()
    strongest = max(
        possible,
        key=lambda item: (_match_strength(item[1], text), -item[0]),
    )
    return TOPICS[strongest[0]]


def _match_strength(query: str, casefolded_text: str) -> int:
    quoted = re.findall(r'"([^"\r\n]+)"', query)
    outside_quotes = re.sub(r'"[^"\r\n]*"', " ", query)
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u3400-\u9fff]+", outside_quotes)
    terms = [*quoted, *(token for token in tokens if token.casefold() != "or")]
    return sum(_term_occurs(term, casefolded_text) for term in terms)


def _term_occurs(term: str, casefolded_text: str) -> bool:
    normalized = term.strip().casefold()
    if not normalized:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", normalized):
        return re.search(
            rf"(?<![\w-]){re.escape(normalized)}(?![\w-])", casefolded_text
        ) is not None
    return normalized in casefolded_text


def _metric(value: int) -> int:
    return max(0, value) if type(value) is int else 0


def _engagement(post: Post, maximum_views: int, maximum_likes: int) -> float:
    views = (
        math.log1p(_metric(post.views)) / math.log1p(maximum_views)
        if maximum_views > 0
        else 0.0
    )
    likes = (
        math.log1p(_metric(post.likes)) / math.log1p(maximum_likes)
        if maximum_likes > 0
        else 0.0
    )
    if maximum_views > 0 and maximum_likes > 0:
        return 0.65 * views + 0.35 * likes
    if maximum_views > 0:
        return views
    if maximum_likes > 0:
        return likes
    return 0.0


def _completeness(post: Post) -> float:
    return (
        0.5 * bool(post.text.strip())
        + 0.3 * bool(post.local_media)
        + 0.2 * bool(post.author.strip() and post.url.strip())
    )
