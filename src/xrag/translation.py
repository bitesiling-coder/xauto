from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Literal, Mapping, Protocol, Sequence

from xrag.models import Post, QuotedPost, TranslationMetadata


_MARKER_PATTERN = re.compile(r"XRAG\d+TOKEN")
_EXCLUDED_PATTERN = re.compile(
    r"https://[^\s]+|`[^`]*`|(?<!\w)[@#$][\w-]+",
    re.UNICODE,
)
_LATIN_PATTERN = re.compile(r"[A-Za-z]")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")

_GLOSSARY = (
    "Autonomous AI Agents",
    "AI Safety Evaluation",
    "Stablecoin Payments",
    "Intelligence Explosion",
    "Prediction Markets",
    "Open-weight Models",
    "Tokenized Stocks",
    "Rogue AI Agents",
    "Humanoid Robots",
    "AI Cybersecurity",
    "On-chain Perps",
    "Agent Security",
    "Embodied AI",
    "World Models",
    "CLARITY Act",
    "Crypto ETF",
    "AI Agent",
    "Solana",
    "MiCA",
    "AGI",
    "RWA",
    "ETP",
    "x402",
)
_GLOSSARY_PATTERNS = tuple(
    re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])",
        re.IGNORECASE,
    )
    for term in _GLOSSARY
)

_RESTORE_ERROR = "protected translation spans were not preserved"


class TranslationEngine(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def revision(self) -> str: ...

    def preflight(self) -> None: ...

    def translate_many(self, texts: Sequence[str]) -> list[str]: ...


@dataclass(frozen=True)
class ProtectedText:
    text: str
    replacements: Mapping[str, str]

    def restore(self, translated: str) -> str:
        if not isinstance(translated, str):
            raise ValueError(_RESTORE_ERROR)

        expected = list(self.replacements)
        found = _MARKER_PATTERN.findall(translated)
        baseline_literals = [
            marker
            for marker in _MARKER_PATTERN.findall(self.text)
            if marker not in self.replacements
        ]
        unknown = [
            marker
            for marker in found
            if marker not in self.replacements and marker not in baseline_literals
        ]

        if unknown:
            raise ValueError(_RESTORE_ERROR)
        if any(translated.count(marker) != 1 for marker in expected):
            raise ValueError(_RESTORE_ERROR)
        if Counter(
            marker for marker in found if marker not in self.replacements
        ) != Counter(baseline_literals):
            raise ValueError(_RESTORE_ERROR)

        positions = [translated.index(marker) for marker in expected]
        if positions != sorted(positions):
            raise ValueError(_RESTORE_ERROR)

        restored = translated
        for marker, original in self.replacements.items():
            restored = restored.replace(marker, original)
        return restored.strip()


@dataclass(frozen=True)
class TranslationFailure:
    owner: Literal["post", "quoted"]
    reason: str


@dataclass(frozen=True)
class TranslationOutcome:
    post: Post
    translated: int
    reused: int
    skipped: int
    errors: tuple[TranslationFailure, ...]


def needs_english_translation(text: str) -> bool:
    countable = _EXCLUDED_PATTERN.sub("", text)
    latin = len(_LATIN_PATTERN.findall(countable))
    if latin < 15:
        return False
    cjk = len(_CJK_PATTERN.findall(countable))
    return latin / (latin + cjk) >= 0.60


def protect_text(text: str) -> ProtectedText:
    structural = [
        (match.start(), match.end()) for match in _EXCLUDED_PATTERN.finditer(text)
    ]
    structural.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    selected: list[tuple[int, int]] = []
    for span in structural:
        if not any(_spans_overlap(span, chosen) for chosen in selected):
            selected.append(span)

    glossary: list[tuple[int, int, int]] = []
    for term_index, pattern in enumerate(_GLOSSARY_PATTERNS):
        glossary.extend(
            (match.start(), match.end(), term_index)
            for match in pattern.finditer(text)
        )
    glossary.sort(key=lambda span: (-(span[1] - span[0]), span[0], span[2]))
    for start, end, _term_index in glossary:
        span = (start, end)
        if not any(_spans_overlap(span, chosen) for chosen in selected):
            selected.append(span)
    selected.sort()

    replacements: dict[str, str] = {}
    chunks: list[str] = []
    cursor = 0
    marker_number = 0
    for start, end in selected:
        chunks.append(text[cursor:start])
        while True:
            marker = f"XRAG{marker_number:04d}TOKEN"
            marker_number += 1
            if marker not in text and marker not in replacements:
                break
        chunks.append(marker)
        replacements[marker] = text[start:end]
        cursor = end
    chunks.append(text[cursor:])
    return ProtectedText("".join(chunks), replacements)


@dataclass(frozen=True)
class _Candidate:
    owner: Literal["post", "quoted"]
    text: str
    protected: ProtectedText


class TranslationEnricher:
    def __init__(
        self,
        engine: TranslationEngine,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._engine = engine
        self._clock = clock

    def preflight(self) -> None:
        self._engine.preflight()

    def enrich(self, post: Post, existing: Post | None) -> TranslationOutcome:
        self.preflight()

        translated = 0
        reused = 0
        skipped = 0
        errors: list[TranslationFailure] = []
        candidates: list[_Candidate] = []

        main_text_zh = ""
        main_metadata: TranslationMetadata | None = None
        quoted = post.quoted_post
        quoted_text_zh = "" if quoted is None else quoted.text_zh
        quoted_metadata = None if quoted is None else quoted.translation_zh

        parts: list[
            tuple[
                Literal["post", "quoted"],
                Post | QuotedPost,
                Post | QuotedPost | None,
            ]
        ] = [("post", post, existing if existing is not None else post)]
        if quoted is not None:
            existing_quote = (
                existing.quoted_post if existing is not None else quoted
            )
            parts.append(("quoted", quoted, existing_quote))

        for owner, current, reuse_source in parts:
            if not needs_english_translation(current.text):
                using_existing = existing is not None and (
                    owner == "post" or existing.quoted_post is not None
                )
                if (
                    using_existing
                    and reuse_source is not None
                    and self._translation_matches(reuse_source, current.text)
                ):
                    reused += 1
                    value = reuse_source.text_zh
                    metadata = reuse_source.translation_zh
                elif self._translation_matches(current, current.text):
                    skipped += 1
                    value = current.text_zh
                    metadata = current.translation_zh
                else:
                    skipped += 1
                    value = ""
                    metadata = None
                if owner == "post":
                    main_text_zh, main_metadata = value, metadata
                else:
                    quoted_text_zh, quoted_metadata = value, metadata
                continue

            if reuse_source is not None and self._translation_matches(
                reuse_source, current.text
            ):
                reused += 1
                value = reuse_source.text_zh
                metadata = reuse_source.translation_zh
                if owner == "post":
                    main_text_zh, main_metadata = value, metadata
                else:
                    quoted_text_zh, quoted_metadata = value, metadata
                continue

            candidates.append(_Candidate(owner, current.text, protect_text(current.text)))
            if owner == "post":
                main_text_zh, main_metadata = "", None
            else:
                quoted_text_zh, quoted_metadata = "", None

        successes = self._translate_candidates(candidates)
        for candidate in candidates:
            value = successes.get(candidate.owner)
            if value is None:
                errors.append(TranslationFailure(candidate.owner, "translation failed"))
                continue
            translated += 1
            metadata = self._new_metadata(candidate.text)
            if candidate.owner == "post":
                main_text_zh, main_metadata = value, metadata
            else:
                quoted_text_zh, quoted_metadata = value, metadata

        result_quote = quoted
        if quoted is not None:
            result_quote = replace(
                quoted,
                text_zh=quoted_text_zh,
                translation_zh=quoted_metadata,
            )
        result_post = replace(
            post,
            text_zh=main_text_zh,
            translation_zh=main_metadata,
            quoted_post=result_quote,
        )
        return TranslationOutcome(
            post=result_post,
            translated=translated,
            reused=reused,
            skipped=skipped,
            errors=tuple(errors),
        )

    def _translation_matches(
        self,
        source: Post | QuotedPost,
        current_text: str,
    ) -> bool:
        metadata = source.translation_zh
        return bool(
            source.text_zh.strip()
            and metadata is not None
            and metadata.language == "zh-CN"
            and metadata.model_id == self._engine.model_id
            and metadata.revision == self._engine.revision
            and metadata.source_sha256 == _source_sha256(current_text)
        )

    def _translate_candidates(
        self,
        candidates: list[_Candidate],
    ) -> dict[Literal["post", "quoted"], str]:
        if not candidates:
            return {}

        try:
            batch = self._engine.translate_many(
                [candidate.protected.text for candidate in candidates]
            )
            restored = self._validate_batch(batch, candidates)
        except Exception:
            restored = None
        if restored is not None:
            return {
                candidate.owner: value
                for candidate, value in zip(candidates, restored, strict=True)
            }

        successes: dict[Literal["post", "quoted"], str] = {}
        for candidate in candidates:
            try:
                result = self._engine.translate_many([candidate.protected.text])
                value = self._validate_batch(result, [candidate])[0]
            except Exception:
                continue
            successes[candidate.owner] = value
        return successes

    @staticmethod
    def _validate_batch(
        result: object,
        candidates: list[_Candidate],
    ) -> list[str]:
        if not isinstance(result, list) or len(result) != len(candidates):
            raise ValueError("invalid translation result")
        restored: list[str] = []
        for value, candidate in zip(result, candidates, strict=True):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("invalid translation result")
            restored_value = candidate.protected.restore(value)
            if not restored_value:
                raise ValueError("invalid translation result")
            restored.append(restored_value)
        return restored

    def _new_metadata(self, text: str) -> TranslationMetadata:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)
        return TranslationMetadata(
            language="zh-CN",
            model_id=self._engine.model_id,
            revision=self._engine.revision,
            source_sha256=_source_sha256(text),
            translated_at=now.isoformat().replace("+00:00", "Z"),
        )


def _source_sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]
