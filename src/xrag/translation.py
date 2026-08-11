from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Literal, Protocol, Sequence

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
    replacements: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        immutable = tuple(
            (marker, original) for marker, original in self.replacements
        )
        object.__setattr__(self, "replacements", immutable)

    def restore(self, translated: str) -> str:
        if not isinstance(translated, str):
            raise ValueError(_RESTORE_ERROR)

        expected = tuple(marker for marker, _original in self.replacements)
        actual = tuple(_MARKER_PATTERN.findall(translated))
        if actual != expected:
            raise ValueError(_RESTORE_ERROR)

        replacement_map = dict(self.replacements)
        return _MARKER_PATTERN.sub(
            lambda match: replacement_map[match.group(0)], translated
        ).strip()


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
    original_marker_matches = tuple(_MARKER_PATTERN.finditer(text))
    original_markers = {match.group(0) for match in original_marker_matches}
    candidates = [
        (match.start(), match.end()) for match in _EXCLUDED_PATTERN.finditer(text)
    ]
    for pattern in _GLOSSARY_PATTERNS:
        candidates.extend(
            (match.start(), match.end())
            for match in pattern.finditer(text)
        )
    candidates.extend(
        (match.start(), match.end()) for match in original_marker_matches
    )
    candidates.sort()

    selected: list[tuple[int, int]] = []
    for start, end in candidates:
        if not selected or start >= selected[-1][1]:
            selected.append((start, end))
            continue
        previous_start, previous_end = selected[-1]
        selected[-1] = (previous_start, max(previous_end, end))

    replacements: list[tuple[str, str]] = []
    used_markers: set[str] = set()
    chunks: list[str] = []
    cursor = 0
    marker_number = 0
    for start, end in selected:
        chunks.append(text[cursor:start])
        while True:
            if marker_number > 9_999:
                raise ValueError("too many protected translation spans")
            marker = f"XRAG{marker_number:04d}TOKEN"
            marker_number += 1
            if marker not in original_markers and marker not in used_markers:
                break
        chunks.append(marker)
        replacements.append((marker, text[start:end]))
        used_markers.add(marker)
        cursor = end
    chunks.append(text[cursor:])
    return ProtectedText("".join(chunks), tuple(replacements))


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

        successes: dict[Literal["post", "quoted"], str] = {}
        retry_candidates = candidates
        try:
            batch = self._engine.translate_many(
                [candidate.protected.text for candidate in candidates]
            )
        except Exception:
            pass
        else:
            if isinstance(batch, list) and len(batch) == len(candidates):
                retry_candidates = []
                for value, candidate in zip(batch, candidates, strict=True):
                    try:
                        successes[candidate.owner] = self._validate_item(
                            value, candidate
                        )
                    except Exception:
                        retry_candidates.append(candidate)

        for candidate in retry_candidates:
            try:
                result = self._engine.translate_many([candidate.protected.text])
                if not isinstance(result, list) or len(result) != 1:
                    raise ValueError("invalid translation result")
                value = self._validate_item(result[0], candidate)
            except Exception:
                continue
            successes[candidate.owner] = value
        return successes

    @staticmethod
    def _validate_item(value: object, candidate: _Candidate) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid translation result")
        restored = candidate.protected.restore(value)
        if not restored:
            raise ValueError("invalid translation result")
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
