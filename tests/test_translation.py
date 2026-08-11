from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Callable, Sequence

import pytest

from xrag.markdown_store import MarkdownStore
from xrag.models import Post, QuotedPost, TranslationMetadata, canonical_source_text
from xrag.translation import (
    ProtectedText,
    TranslationEnricher,
    TranslationFailure,
    needs_english_translation,
    protect_text,
)


class FakeEngine:
    model_id = "fake-model"
    revision = "rev-1"

    def __init__(
        self,
        responder: Callable[[list[str], int], object] | None = None,
    ) -> None:
        self.responder = responder or (
            lambda texts, _call: [f"中文 {text}" for text in texts]
        )
        self.calls: list[list[str]] = []
        self.preflight_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def translate_many(self, texts: Sequence[str]) -> list[str]:
        batch = list(texts)
        self.calls.append(batch)
        result = self.responder(batch, len(self.calls))
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def make_post(
    text: str = "Fifteen letters plus enough useful English words",
    quoted_text: str | None = None,
    *,
    text_zh: str = "",
    translation_zh: TranslationMetadata | None = None,
) -> Post:
    quoted = None
    if quoted_text is not None:
        quoted = QuotedPost(
            id="q1",
            author="quoted",
            text=quoted_text,
            created_at="2026-08-10T00:00:00Z",
            url="https://x.com/quoted/status/q1",
        )
    return Post(
        id="p1",
        author="main",
        text=text,
        created_at="2026-08-11T00:00:00Z",
        url="https://x.com/main/status/p1",
        quoted_post=quoted,
        text_zh=text_zh,
        translation_zh=translation_zh,
    )


def metadata_for(
    text: str,
    *,
    revision: str = "rev-1",
    model_id: str = "fake-model",
    language: str = "zh-CN",
) -> TranslationMetadata:
    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return TranslationMetadata(
        language=language,
        model_id=model_id,
        revision=revision,
        source_sha256=digest,
        translated_at="2026-08-01T00:00:00Z",
    )


def test_enrich_cr_newlines_produces_metadata_that_round_trips_through_markdown(
    tmp_path,
) -> None:
    main_text = " Main English text has enough words\r\nfor translation and a lone\rreturn "
    quoted_text = " Quoted English text also has enough words\rfor a real translation\r\n "
    outcome = TranslationEnricher(FakeEngine()).enrich(
        make_post(main_text, quoted_text), existing=None
    )

    assert outcome.post.translation_zh is not None
    assert outcome.post.translation_zh.source_sha256 == hashlib.sha256(
        canonical_source_text(main_text).encode("utf-8")
    ).hexdigest()
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.translation_zh is not None
    assert outcome.post.quoted_post.translation_zh.source_sha256 == hashlib.sha256(
        canonical_source_text(quoted_text).encode("utf-8")
    ).hexdigest()

    path = MarkdownStore(tmp_path).upsert(outcome.post)
    content = path.read_bytes()
    reread = MarkdownStore(tmp_path).read(path)

    assert b"\r" not in content
    assert reread.text == canonical_source_text(main_text)
    assert reread.quoted_post is not None
    assert reread.quoted_post.text == canonical_source_text(quoted_text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abcdefghijklmn", False),
        ("abcdefghijklmno", True),
        ("abcdefghijklmno一丁丂七丄丅丆万丈三", True),
        ("abcdefghijklmno一丁丂七丄丅丆万丈三上", False),
        ("", False),
        ("这是一段中文 AI RWA 不应该被识别为英文主导内容", False),
        (
            "https://abcdefghijklmnopqrstuvwxyz.example/path "
            "@verylongusername #VeryLongHashTag $VERYLONGTOKEN "
            "`veryLongCodeSnippetHere`",
            False,
        ),
        ("Autonomous AI Agents", True),
    ],
)
def test_needs_english_translation_boundaries(text: str, expected: bool) -> None:
    assert needs_english_translation(text) is expected


@pytest.mark.parametrize(
    "span",
    [
        "https://example.com/a?b=c",
        "@OpenAI",
        "#AISafety",
        "$RWA",
        "`print('secret')`",
    ],
)
def test_protect_text_preserves_each_structural_span(span: str) -> None:
    original = f"Before {span} after"
    protected = protect_text(original)

    assert span not in protected.text
    assert protected.restore(protected.text) == original


@pytest.mark.parametrize(
    "term",
    [
        "AI Agent",
        "Autonomous AI Agents",
        "Rogue AI Agents",
        "Agent Security",
        "AI Safety Evaluation",
        "World Models",
        "Embodied AI",
        "Humanoid Robots",
        "AI Cybersecurity",
        "Open-weight Models",
        "AGI",
        "Intelligence Explosion",
        "RWA",
        "Tokenized Stocks",
        "Stablecoin Payments",
        "Prediction Markets",
        "x402",
        "Solana",
        "On-chain Perps",
        "Crypto ETF",
        "ETP",
        "MiCA",
        "CLARITY Act",
    ],
)
def test_protect_text_preserves_every_glossary_term(term: str) -> None:
    protected = protect_text(f"Discuss {term} now")

    assert term not in protected.text
    assert protected.restore(protected.text) == f"Discuss {term} now"


def test_protect_text_uses_longest_overlapping_term_and_original_casing() -> None:
    original = "Autonomous AI Agents meet ai agent and AI Agent"
    protected = protect_text(original)
    replacement_values = [original for _marker, original in protected.replacements]

    assert protected.restore(protected.text) == original
    assert "Autonomous AI Agents" in replacement_values
    assert "ai agent" in replacement_values
    assert "AI Agent" in replacement_values


def test_protect_text_merges_overlapping_terms_into_one_protected_span() -> None:
    original = "AI Agent Security"

    protected = protect_text(original)

    assert protected.replacements == (("XRAG0000TOKEN", original),)
    assert protected.restore(protected.text) == original


def test_protect_text_merges_structural_token_with_overlapping_term() -> None:
    original = "@Autonomous AI Agents"

    protected = protect_text(original)

    assert protected.replacements == (("XRAG0000TOKEN", original),)
    assert protected.restore(protected.text) == original


def test_protect_text_avoids_marker_collision_in_original() -> None:
    original = "XRAG0000TOKEN then AI Agent"
    protected = protect_text(original)

    assert "XRAG0001TOKEN" in protected.text
    assert "XRAG0002TOKEN" in protected.text
    assert "XRAG0000TOKEN" not in protected.text
    assert protected.restore(protected.text) == original


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text, markers: text.replace(markers[0], ""),
        lambda text, markers: text.replace(markers[0], markers[0] + markers[0]),
        lambda text, markers: text.replace(
            markers[0], "XRAG9999TOKEN", 1
        ),
        lambda text, markers: text.replace(markers[0], "TEMP", 1)
        .replace(markers[1], markers[0], 1)
        .replace("TEMP", markers[1], 1),
    ],
)
def test_restore_rejects_lost_repeated_unknown_or_reordered_markers(
    mutate: Callable[[str, list[str]], str],
) -> None:
    protected = protect_text("AI Agent and RWA")
    markers = [marker for marker, _original in protected.replacements]

    with pytest.raises(
        ValueError, match="^protected translation spans were not preserved$"
    ):
        protected.restore(mutate(protected.text, markers))


def test_restore_rejects_unknown_marker_with_more_than_four_digits() -> None:
    protected = protect_text("AI Agent")

    with pytest.raises(
        ValueError, match="^protected translation spans were not preserved$"
    ):
        protected.restore(f"{protected.text} XRAG12345TOKEN")


def test_protected_text_and_failure_are_frozen() -> None:
    protected = ProtectedText("plain", ())
    failure = TranslationFailure("post", "translation failed")

    with pytest.raises(FrozenInstanceError):
        protected.text = "changed"
    with pytest.raises(FrozenInstanceError):
        failure.reason = "changed"


def test_protected_text_replacements_are_an_immutable_tuple() -> None:
    protected = ProtectedText(
        "XRAG0000TOKEN",
        (("XRAG0000TOKEN", "AI Agent"),),
    )
    assert protected.replacements == (("XRAG0000TOKEN", "AI Agent"),)
    assert not hasattr(protected.replacements, "clear")
    with pytest.raises(TypeError):
        protected.replacements[0] = ("XRAG0000TOKEN", "changed")


@pytest.mark.parametrize(
    ("text", "replacements"),
    [
        ("plain", [["XRAG0000TOKEN", "value"]]),
        ("plain", (["XRAG0000TOKEN", "value"],)),
        ("plain", (("XRAG0000TOKEN",),)),
        ("plain", (("not-a-marker", "value"),)),
        ("plain", (("XRAG0000TOKEN", 123),)),
        (
            "XRAG0000TOKEN XRAG0000TOKEN",
            (
                ("XRAG0000TOKEN", "first"),
                ("XRAG0000TOKEN", "second"),
            ),
        ),
        (123, ()),
    ],
)
def test_protected_text_rejects_invalid_manual_construction(
    text: object,
    replacements: object,
) -> None:
    with pytest.raises(
        ValueError, match="^protected translation spans were not preserved$"
    ):
        ProtectedText(
            text,  # type: ignore[arg-type]
            replacements,  # type: ignore[arg-type]
        )


def test_protect_and_restore_large_number_of_tokens() -> None:
    original = " ".join(f"@user{index}" for index in range(8_000))

    protected = protect_text(original)

    assert isinstance(protected.replacements, tuple)
    assert len(protected.replacements) == 8_000
    assert protected.restore(protected.text) == original


def test_more_than_ten_thousand_spans_protect_restore_and_enrich() -> None:
    text = "``" * 10_001 + " abcdefghijklmno"
    engine = FakeEngine(lambda texts, _call: [f"中文 {item}" for item in texts])

    assert needs_english_translation(text) is True
    protected = protect_text(text)
    assert len(protected.replacements) == 10_001
    assert protected.restore(protected.text) == text

    outcome = TranslationEnricher(engine).enrich(make_post(text), existing=None)

    assert outcome.translated == 1
    assert outcome.errors == ()
    assert outcome.post.text_zh == f"中文 {text}"


def test_enrich_batches_main_and_quoted_and_attaches_metadata() -> None:
    engine = FakeEngine()
    clock = lambda: datetime(2026, 8, 11, 12, 34, 56, tzinfo=timezone.utc)
    post = make_post(quoted_text="Quoted English content has many letters")

    outcome = TranslationEnricher(engine, clock=clock).enrich(post, existing=None)

    assert engine.preflight_calls == 1
    assert len(engine.calls) == 1
    assert len(engine.calls[0]) == 2
    assert outcome.translated == 2
    assert outcome.reused == outcome.skipped == 0
    assert outcome.errors == ()
    assert outcome.post.text_zh.startswith("中文 ")
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh.startswith("中文 ")
    assert outcome.post.translation_zh is not None
    assert outcome.post.translation_zh.language == "zh-CN"
    assert outcome.post.translation_zh.model_id == "fake-model"
    assert outcome.post.translation_zh.revision == "rev-1"
    assert outcome.post.translation_zh.source_sha256 == metadata_for(
        post.text
    ).source_sha256
    assert outcome.post.translation_zh.translated_at == "2026-08-11T12:34:56Z"


def test_enrich_strictly_reuses_matching_existing_translations_without_model_call() -> None:
    main_text = "Current English main post contains enough letters"
    quote_text = "Current quoted English post contains enough letters"
    incoming = make_post(main_text, quote_text)
    existing = make_post(
        main_text,
        quote_text,
        text_zh="已有主帖翻译",
        translation_zh=metadata_for(main_text),
    )
    assert existing.quoted_post is not None
    existing = Post(
        **{
            **existing.__dict__,
            "quoted_post": QuotedPost(
                **{
                    **existing.quoted_post.__dict__,
                    "text_zh": "已有引用翻译",
                    "translation_zh": metadata_for(quote_text),
                }
            ),
        }
    )
    engine = FakeEngine()

    outcome = TranslationEnricher(engine).enrich(incoming, existing)

    assert engine.calls == []
    assert engine.preflight_calls == 1
    assert outcome.translated == 0
    assert outcome.reused == 2
    assert outcome.post.text_zh == "已有主帖翻译"
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh == "已有引用翻译"


@pytest.mark.parametrize(
    "bad_metadata",
    [
        metadata_for(
            "Current English main post contains enough letters", revision="old"
        ),
        metadata_for("Different English source contains enough letters"),
    ],
)
def test_enrich_retranslates_when_revision_or_source_changes(
    bad_metadata: TranslationMetadata,
) -> None:
    text = "Current English main post contains enough letters"
    post = make_post(text, text_zh="陈旧译文", translation_zh=bad_metadata)
    engine = FakeEngine()

    outcome = TranslationEnricher(engine).enrich(post, existing=None)

    assert len(engine.calls) == 1
    assert outcome.translated == 1
    assert outcome.reused == 0
    assert outcome.post.text_zh != "陈旧译文"


def test_enrich_skips_non_english_and_clears_stale_translation() -> None:
    post = make_post(
        "这是一段中文内容",
        text_zh="陈旧译文",
        translation_zh=metadata_for("其他来源"),
    )
    engine = FakeEngine()

    outcome = TranslationEnricher(engine).enrich(post, existing=None)

    assert engine.calls == []
    assert outcome.skipped == 1
    assert outcome.post.text_zh == ""
    assert outcome.post.translation_zh is None


def test_enrich_skips_non_english_but_keeps_self_consistent_translation() -> None:
    text = "这是一段中文内容"
    metadata = metadata_for(text)
    post = make_post(text, text_zh="已有译文", translation_zh=metadata)

    outcome = TranslationEnricher(FakeEngine()).enrich(post, existing=None)

    assert outcome.skipped == 1
    assert outcome.post.text_zh == "已有译文"
    assert outcome.post.translation_zh is metadata


def test_enrich_reuses_matching_existing_translation_for_non_english() -> None:
    text = "这是一段中文内容"
    incoming = make_post(text)
    existing = make_post(
        text,
        text_zh="已有中文译文",
        translation_zh=metadata_for(text),
    )
    engine = FakeEngine()

    outcome = TranslationEnricher(engine).enrich(incoming, existing)

    assert engine.calls == []
    assert outcome.reused == 1
    assert outcome.skipped == 0
    assert outcome.post.text_zh == "已有中文译文"
    assert outcome.post.translation_zh == existing.translation_zh


def test_enrich_skips_non_english_and_clears_stale_existing_translation() -> None:
    text = "这是一段中文内容"
    incoming = make_post(text)
    existing = make_post(
        text,
        text_zh="陈旧中文译文",
        translation_zh=metadata_for(text, revision="old-revision"),
    )
    engine = FakeEngine()

    outcome = TranslationEnricher(engine).enrich(incoming, existing)

    assert engine.calls == []
    assert outcome.reused == 0
    assert outcome.skipped == 1
    assert outcome.post.text_zh == ""
    assert outcome.post.translation_zh is None


def test_batch_exception_retries_each_item_so_quote_can_succeed() -> None:
    def respond(texts: list[str], call: int) -> object:
        if call == 1:
            return RuntimeError("batch unavailable")
        if "Main" in texts[0]:
            return RuntimeError("private main content")
        return [f"引用成功 {texts[0]}"]

    engine = FakeEngine(respond)
    post = make_post(
        "Main English content has enough private letters",
        "Quoted English content has enough useful letters",
    )

    outcome = TranslationEnricher(engine).enrich(post, existing=None)

    assert [len(call) for call in engine.calls] == [2, 1, 1]
    assert outcome.translated == 1
    assert outcome.post.text_zh == ""
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh.startswith("引用成功")
    assert outcome.errors == (TranslationFailure("post", "translation failed"),)
    assert "private" not in repr(outcome.errors)


def test_one_blank_batch_result_retries_only_invalid_item() -> None:
    def respond(texts: list[str], call: int) -> object:
        if call == 1:
            return ["   ", "batch quote"]
        return ["   "]

    engine = FakeEngine(respond)
    outcome = TranslationEnricher(engine).enrich(
        make_post(
            "Main English content has enough useful letters",
            "Quoted English content has enough useful letters",
        ),
        existing=None,
    )

    assert [len(call) for call in engine.calls] == [2, 1]
    assert outcome.translated == 1
    assert outcome.errors == (TranslationFailure("post", "translation failed"),)
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh == "batch quote"


def test_valid_main_batch_result_survives_failed_retry_for_blank_quote() -> None:
    def respond(_texts: list[str], call: int) -> object:
        if call == 1:
            return ["主帖 batch 成功", "   "]
        return RuntimeError("single retry unavailable")

    engine = FakeEngine(respond)
    outcome = TranslationEnricher(engine).enrich(
        make_post(
            "Main English content has enough useful letters",
            "Quoted English content has enough useful letters",
        ),
        existing=None,
    )

    assert [len(call) for call in engine.calls] == [2, 1]
    assert outcome.translated == 1
    assert outcome.post.text_zh == "主帖 batch 成功"
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh == ""
    assert outcome.errors == (TranslationFailure("quoted", "translation failed"),)


def test_valid_quote_batch_result_survives_failed_retry_for_bad_main_marker() -> None:
    def respond(_texts: list[str], call: int) -> object:
        if call == 1:
            return ["XRAG9999TOKEN", "引用 batch 成功"]
        return RuntimeError("single retry unavailable")

    engine = FakeEngine(respond)
    outcome = TranslationEnricher(engine).enrich(
        make_post(
            "Main English content has enough useful letters",
            "Quoted English content has enough useful letters",
        ),
        existing=None,
    )

    assert [len(call) for call in engine.calls] == [2, 1]
    assert outcome.translated == 1
    assert outcome.post.text_zh == ""
    assert outcome.post.quoted_post is not None
    assert outcome.post.quoted_post.text_zh == "引用 batch 成功"
    assert outcome.errors == (TranslationFailure("post", "translation failed"),)


@pytest.mark.parametrize("invalid", [None, "not-a-list", [123], []])
def test_invalid_engine_results_become_sanitized_failure(invalid: object) -> None:
    engine = FakeEngine(lambda _texts, _call: invalid)
    sensitive = "Sensitive English body contains enough letters"

    outcome = TranslationEnricher(engine).enrich(make_post(sensitive), existing=None)

    assert outcome.post.text_zh == ""
    assert outcome.errors == (TranslationFailure("post", "translation failed"),)
    assert sensitive not in repr(outcome.errors)


def test_enrich_does_not_mutate_input() -> None:
    post = make_post(quoted_text="Quoted English content has many letters")
    before = post

    outcome = TranslationEnricher(FakeEngine()).enrich(post, existing=None)

    assert post == before
    assert outcome.post is not post
    assert outcome.post.quoted_post is not post.quoted_post


@pytest.mark.parametrize(
    ("clock_value", "expected"),
    [
        (datetime(2026, 8, 11, 1, 2, 3), "2026-08-11T01:02:03Z"),
        (
            datetime(2026, 8, 11, 9, 2, 3, tzinfo=timezone(timedelta(hours=8))),
            "2026-08-11T01:02:03Z",
        ),
    ],
)
def test_enrich_normalizes_naive_and_aware_clock_to_utc_rfc3339(
    clock_value: datetime,
    expected: str,
) -> None:
    outcome = TranslationEnricher(FakeEngine(), clock=lambda: clock_value).enrich(
        make_post(), existing=None
    )

    assert outcome.post.translation_zh is not None
    assert outcome.post.translation_zh.translated_at == expected


def test_enricher_preflight_delegates_to_engine() -> None:
    engine = FakeEngine()

    TranslationEnricher(engine).preflight()

    assert engine.preflight_calls == 1
