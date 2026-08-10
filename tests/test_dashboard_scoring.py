from __future__ import annotations

import math
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xrag.dashboard_scoring import TOPICS, RankedPost, Topic, rank_posts
from xrag.models import LocalMedia, Post


QUERIES = (
    '"Autonomous AI Agents" OR "Agent Security"',
    '"World Models" OR Embodied AI',
    'RWA OR "Stablecoin Payments"',
    '"Prediction Markets" OR MiCA',
)
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=ZoneInfo("Asia/Singapore"))


def post(
    post_id: str,
    *,
    text: str = "Agent Security",
    created_at: datetime | str = NOW,
    author: str = "Ada",
    url: str | None = None,
    likes: int = 0,
    views: int = 0,
    source_keywords: tuple[str, ...] = (QUERIES[0],),
    local_media: tuple[LocalMedia, ...] = (),
) -> Post:
    timestamp = created_at.isoformat() if isinstance(created_at, datetime) else created_at
    return Post(
        id=post_id,
        author=author,
        text=text,
        created_at=timestamp,
        url=f"https://x.com/status/{post_id}" if url is None else url,
        likes=likes,
        views=views,
        source_keywords=source_keywords,
        local_media=local_media,
    )


def test_topics_have_exact_public_identity_and_are_immutable() -> None:
    assert TOPICS == (
        Topic("ai-agents-security", "AI Agents 与 Agent Security", "AI"),
        Topic("world-models-embodied-ai", "World Models 与 Embodied AI", "AI"),
        Topic("rwa-stablecoin-payments", "RWA 与 Stablecoin Payments", "Web3"),
        Topic(
            "prediction-markets-regulation",
            "Prediction Markets 与 Crypto Regulation",
            "Web3",
        ),
    )
    with pytest.raises(FrozenInstanceError):
        TOPICS[0].label = "changed"  # type: ignore[misc]
    ranked = RankedPost(
        post=post("immutable"),
        topic=TOPICS[0],
        score=1.0,
        engagement=1.0,
        freshness=1.0,
        topic_frequency=1.0,
        completeness=1.0,
        fallback=False,
    )
    with pytest.raises(FrozenInstanceError):
        ranked.score = 0.0  # type: ignore[misc]


def test_generated_dashboard_site_is_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[1]

    assert "data/dashboard-site/" in root.joinpath(".gitignore").read_text(
        encoding="utf-8"
    ).splitlines()


def test_multiple_sourced_topics_use_query_match_strength() -> None:
    item = post(
        "multi",
        text="World Models will shape Embodied AI systems and world models research.",
        source_keywords=(QUERIES[0], QUERIES[1]),
    )

    ranked = rank_posts(
        [item],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert ranked[0].topic == TOPICS[1]


def test_cjk_query_tokens_affect_multi_topic_match_strength() -> None:
    queries = (
        '"Agent Security" OR 智能体安全',
        '"World Models" OR 世界模型 OR 具身智能',
    )
    item = post(
        "cjk-multi",
        text="世界模型正在推动具身智能研究。",
        source_keywords=(queries[0], queries[1]),
    )

    ranked = rank_posts(
        [item],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=queries,
        minimum_today=1,
    )

    assert ranked[0].topic == TOPICS[1]


def test_same_day_components_and_score_follow_formula() -> None:
    items = [
        post(
            "target",
            text="World Models",
            created_at=NOW - timedelta(hours=12),
            author="",
            url="",
            views=9,
            likes=3,
            source_keywords=(QUERIES[1],),
        ),
        post("world-other", text="Embodied", views=99, likes=9, source_keywords=(QUERIES[1],)),
        post("agent-1", views=1, likes=1),
        post("agent-2", views=1, likes=1),
        post("agent-3", views=1, likes=1),
        post("agent-4", views=1, likes=1),
    ]

    ranked = rank_posts(items, now=NOW, timezone_name="Asia/Singapore", configured_keywords=QUERIES)
    target = next(item for item in ranked if item.post.id == "target")
    expected_engagement = 0.65 * math.log1p(9) / math.log1p(99) + 0.35 * math.log1p(3) / math.log1p(9)
    expected_score = 0.40 * expected_engagement + 0.30 * 0.75 + 0.20 * 0.5 + 0.10 * 0.5

    assert target.engagement == pytest.approx(expected_engagement)
    assert target.freshness == pytest.approx(0.75)
    assert target.topic_frequency == pytest.approx(0.5)
    assert target.completeness == pytest.approx(0.5)
    assert target.score == pytest.approx(expected_score)
    assert all(
        0.0 <= component <= 1.0
        for item in ranked
        for component in (
            item.score,
            item.engagement,
            item.freshness,
            item.topic_frequency,
            item.completeness,
        )
    )
    assert all(not item.fallback for item in ranked)


def test_fallback_uses_rolling_window_excludes_old_posts_and_labels_only_prior_day() -> None:
    items = [
        post("today", created_at=NOW - timedelta(hours=1)),
        post("prior", created_at=NOW - timedelta(hours=20)),
        post("old", created_at=NOW - timedelta(hours=48, seconds=1)),
    ]

    ranked = rank_posts(items, now=NOW, timezone_name="Asia/Singapore", configured_keywords=QUERIES)

    assert {item.post.id for item in ranked} == {"today", "prior"}
    assert {item.post.id: item.fallback for item in ranked} == {"today": False, "prior": True}


def test_x_native_timestamps_participate_in_today_and_48_hour_fallback() -> None:
    items = [
        post("x-today", created_at="Mon Aug 10 03:00:37 +0000 2026"),
        post("x-prior", created_at="Sun Aug 09 15:00:00 +0000 2026"),
    ]

    today_only = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )
    fallback = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=2,
    )

    assert [item.post.id for item in today_only] == ["x-today"]
    assert today_only[0].fallback is False
    assert {item.post.id: item.fallback for item in fallback} == {
        "x-today": False,
        "x-prior": True,
    }


def test_passed_timezone_controls_calendar_day_across_utc_boundary() -> None:
    singapore = ZoneInfo("Asia/Singapore")
    local_now = datetime(2026, 8, 10, 10, 0, tzinfo=singapore)
    same_day = datetime(2026, 8, 10, 0, 30, tzinfo=singapore)
    prior_day = datetime(2026, 8, 9, 23, 30, tzinfo=singapore)
    assert local_now.astimezone(timezone.utc).date() != same_day.astimezone(
        timezone.utc
    ).date()
    assert same_day.astimezone(timezone.utc).date() == prior_day.astimezone(
        timezone.utc
    ).date()
    items = [
        post(
            "same-singapore-day",
            created_at=same_day,
        ),
        post(
            "prior-singapore-day",
            created_at=prior_day,
        ),
    ]

    today_only = rank_posts(
        items,
        now=local_now,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )
    fallback = rank_posts(
        items,
        now=local_now,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=2,
    )

    assert [item.post.id for item in today_only] == ["same-singapore-day"]
    assert today_only[0].fallback is False
    assert {item.post.id: item.fallback for item in fallback} == {
        "same-singapore-day": False,
        "prior-singapore-day": True,
    }


def test_deduplicates_id_then_normalized_url_keeping_more_complete_record() -> None:
    media = (
        LocalMedia(
            "post",
            "image",
            "https://pbs.twimg.com/media/a",
            "../media/a.jpg",
            "image/jpeg",
        ),
    )
    items = [
        post("Same", text="", author="", url="https://x.com/status/id", views=999),
        post("same", text="complete", url="https://x.com/status/id-better", local_media=media),
        post("url-poor", text="", author="", url=" HTTPS://X.COM/STATUS/SHARED ", views=999),
        post("url-rich", text="complete", url="https://x.com/status/shared", local_media=media),
    ]

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert {item.post.id for item in ranked} == {"same", "url-rich"}


def test_ineligible_richer_id_duplicate_cannot_suppress_valid_candidate() -> None:
    media = (
        LocalMedia(
            "post",
            "image",
            "https://pbs.twimg.com/media/richer-id",
            "../media/richer-id.jpg",
            "image/jpeg",
        ),
    )
    valid = post(
        "same-id",
        text="valid",
        author="",
        url="https://x.com/status/valid-id",
    )
    richer_but_invalid = post(
        "SAME-ID",
        text="richer but invalid",
        created_at="not-a-date",
        url="https://x.com/status/invalid-id",
        local_media=media,
    )

    ranked = rank_posts(
        [valid, richer_but_invalid],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert [item.post for item in ranked] == [valid]


def test_ineligible_richer_url_duplicate_cannot_suppress_valid_candidate() -> None:
    media = (
        LocalMedia(
            "post",
            "image",
            "https://pbs.twimg.com/media/richer-url",
            "../media/richer-url.jpg",
            "image/jpeg",
        ),
    )
    valid = post(
        "valid-url",
        text="valid",
        author="",
        url="https://x.com/status/shared-eligibility",
    )
    richer_but_unknown = post(
        "unknown-url",
        text="richer but unknown",
        url=" HTTPS://X.COM/STATUS/SHARED-ELIGIBILITY ",
        source_keywords=("not configured",),
        local_media=media,
    )

    ranked = rank_posts(
        [valid, richer_but_unknown],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert [item.post for item in ranked] == [valid]


@pytest.mark.parametrize("dedupe_path", ["id", "url"])
@pytest.mark.parametrize(
    ("loser_values", "winner_values"),
    [
        (
            {"text": "same-a", "views": 5, "likes": 100},
            {"text": "same-b", "views": 6, "likes": 0},
        ),
        (
            {"text": "same-a", "views": 6, "likes": 5},
            {"text": "same-b", "views": 6, "likes": 6},
        ),
        (
            {"text": "short", "views": 6, "likes": 6},
            {"text": "longer", "views": 6, "likes": 6},
        ),
    ],
    ids=["views", "likes", "text-length"],
)
def test_dedupe_ties_use_views_then_likes_then_text_length(
    dedupe_path: str,
    loser_values: dict[str, object],
    winner_values: dict[str, object],
) -> None:
    if dedupe_path == "id":
        loser = post(
            "DUPLICATE",
            author="loser",
            url="https://x.com/status/loser",
            **loser_values,  # type: ignore[arg-type]
        )
        winner = post(
            "duplicate",
            author="winner",
            url="https://x.com/status/winner",
            **winner_values,  # type: ignore[arg-type]
        )
    else:
        loser = post(
            "loser",
            author="loser",
            url=" HTTPS://X.COM/STATUS/SHARED ",
            **loser_values,  # type: ignore[arg-type]
        )
        winner = post(
            "winner",
            author="winner",
            url="https://x.com/status/shared",
            **winner_values,  # type: ignore[arg-type]
        )

    ranked = rank_posts(
        [loser, winner],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert len(ranked) == 1
    assert ranked[0].post.author == "winner"


def test_enforces_casefolded_author_cap_and_result_limit() -> None:
    items = [
        post(f"ada-{index}", author="Ada" if index % 2 else "ADA", views=100 - index)
        for index in range(5)
    ]
    items.extend(post(f"bob-{index}", author="Bob", views=50 - index) for index in range(5))

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
        limit=4,
        max_per_author=3,
    )

    assert len(ranked) == 4
    assert sum(item.post.author.casefold() == "ada" for item in ranked) == 3


def test_author_cap_strips_whitespace_and_casefolds_to_one_quota() -> None:
    items = [
        post("ada-1", author="Ada", views=100),
        post("ada-2", author=" Ada ", views=90),
        post("ada-3", author="ADA", views=80),
        post("ada-4", author="ada", views=70),
        post("bob", author="Bob", views=60),
    ]

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
        limit=5,
        max_per_author=3,
    )

    assert len(ranked) == 4
    assert sum(item.post.author.strip().casefold() == "ada" for item in ranked) == 3
    assert any(item.post.id == "bob" for item in ranked)


def test_equal_topic_strength_uses_configured_priority_not_source_order() -> None:
    item = post(
        "tie",
        text="Agent Security and World Models",
        source_keywords=(QUERIES[1], QUERIES[0]),
    )

    ranked = rank_posts(
        [item],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert ranked[0].topic == TOPICS[0]


def test_invalid_naive_and_too_future_dates_are_ignored() -> None:
    items = [
        post("valid"),
        post("invalid", created_at="not-a-date"),
        post("naive", created_at="2026-08-10T11:00:00"),
        post("x-naive", created_at="Mon Aug 10 03:00:37 2026"),
        post("x-incomplete", created_at="Mon Aug 10 03:00 +0000 2026"),
        post("future", created_at=NOW + timedelta(minutes=5, seconds=1)),
    ]

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert [item.post.id for item in ranked] == ["valid"]


def test_posts_without_known_configured_topic_are_ignored() -> None:
    extended = (*QUERIES, "fifth unknown topic")
    items = [
        post("not-sourced", source_keywords=("not configured",)),
        post("case-mismatch", source_keywords=(QUERIES[0].lower(),)),
        post("beyond-four", text="fifth unknown topic", source_keywords=(extended[4],)),
        post("known"),
    ]

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=extended,
        minimum_today=1,
    )

    assert [item.post.id for item in ranked] == ["known"]


def test_rejects_naive_clock_and_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        rank_posts(
            [],
            now=NOW.replace(tzinfo=None),
            timezone_name="Asia/Singapore",
            configured_keywords=QUERIES,
        )
    with pytest.raises(ValueError, match="timezone"):
        rank_posts(
            [],
            now=NOW,
            timezone_name="Mars/Olympus",
            configured_keywords=QUERIES,
        )


@pytest.mark.parametrize(
    "argument",
    [
        {"minimum_today": 0},
        {"window_hours": 0},
        {"limit": 0},
        {"max_per_author": 0},
        {"minimum_today": -1},
        {"window_hours": -1},
        {"limit": -1},
        {"max_per_author": -1},
    ],
)
def test_rejects_nonpositive_numeric_limits(argument: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive"):
        rank_posts(
            [],
            now=NOW,
            timezone_name="Asia/Singapore",
            configured_keywords=QUERIES,
            **argument,
        )


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"views": 9, "likes": 0}, math.log1p(9) / math.log1p(99)),
        ({"views": 0, "likes": 3}, math.log1p(3) / math.log1p(9)),
    ],
)
def test_missing_metric_redistributes_engagement_weight(
    metrics: dict[str, int], expected: float
) -> None:
    target = post("target", **metrics)
    maximum = post(
        "maximum",
        views=99 if metrics["views"] else 0,
        likes=9 if metrics["likes"] else 0,
    )

    ranked = rank_posts(
        [target, maximum],
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    result = next(item for item in ranked if item.post.id == "target")
    assert result.engagement == pytest.approx(expected)


def test_sort_ties_by_time_then_stable_post_id() -> None:
    items = [
        post("b", views=1),
        post("a", views=1),
        post("newer", created_at=NOW + timedelta(minutes=5), views=1),
    ]

    ranked = rank_posts(
        items,
        now=NOW,
        timezone_name="Asia/Singapore",
        configured_keywords=QUERIES,
        minimum_today=1,
    )

    assert [item.post.id for item in ranked] == ["newer", "a", "b"]
