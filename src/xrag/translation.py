from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import re
from typing import Callable, Literal, Protocol, Sequence

from xrag.models import Post, QuotedPost, TranslationMetadata, canonical_source_text


_MARKER_PATTERN = re.compile(r"XRAG\d+TOKEN")
_EXCLUDED_PATTERN = re.compile(
    r"https://[^\s]+|`[^`]*`|(?<!\w)[@#$][\w-]+",
    re.UNICODE,
)
_LATIN_PATTERN = re.compile(r"[A-Za-z]")
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_VIETNAMESE_PATTERN = re.compile(r"[\u0102\u0103\u00c2\u00e2\u0110\u0111\u00ca\u00ea\u00d4\u00f4\u01a0\u01a1\u01af\u01b0\u1ea0-\u1ef9]")

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
_DEFAULT_BATCH_SIZE = 16


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
        if not isinstance(self.text, str) or type(self.replacements) is not tuple:
            raise ValueError(_RESTORE_ERROR)

        markers: set[str] = set()
        for replacement in self.replacements:
            if type(replacement) is not tuple or len(replacement) != 2:
                raise ValueError(_RESTORE_ERROR)
            marker, original = replacement
            if (
                not isinstance(marker, str)
                or _MARKER_PATTERN.fullmatch(marker) is None
                or marker in markers
                or not isinstance(original, str)
            ):
                raise ValueError(_RESTORE_ERROR)
            markers.add(marker)

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

    def translatable_segments(self) -> tuple[str, ...]:
        expected = tuple(marker for marker, _original in self.replacements)
        if tuple(_MARKER_PATTERN.findall(self.text)) != expected:
            raise ValueError(_RESTORE_ERROR)
        return tuple(_MARKER_PATTERN.split(self.text))

    def restore_segments(self, translated_segments: Sequence[str]) -> str:
        if isinstance(translated_segments, (str, bytes)) or not isinstance(
            translated_segments, Sequence
        ):
            raise ValueError(_RESTORE_ERROR)

        source_segments = self.translatable_segments()
        values = list(translated_segments)
        if len(values) != len(source_segments):
            raise ValueError(_RESTORE_ERROR)
        for source, translated in zip(source_segments, values, strict=True):
            if not isinstance(translated, str):
                raise ValueError(_RESTORE_ERROR)
            if source.strip():
                if not translated.strip() or _MARKER_PATTERN.search(translated):
                    raise ValueError(_RESTORE_ERROR)
            elif translated != source:
                raise ValueError(_RESTORE_ERROR)

        chunks = [values[0]]
        for (_marker, original), translated in zip(
            self.replacements, values[1:], strict=True
        ):
            chunks.extend((original, translated))
        return "".join(chunks).strip()


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
    if len(_VIETNAMESE_PATTERN.findall(countable)) >= 2:
        return False
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


@dataclass(frozen=True)
class _Segment:
    owner: Literal["post", "quoted"]
    index: int
    text: str


class TranslationEnricher:
    def __init__(
        self,
        engine: TranslationEngine,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch size must be a positive integer")
        self._engine = engine
        self._clock = clock
        self._batch_size = batch_size

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
        outputs: dict[Literal["post", "quoted"], list[str]] = {}
        pending: list[_Segment] = []
        completed: set[tuple[Literal["post", "quoted"], int]] = set()
        for candidate in candidates:
            segments = candidate.protected.translatable_segments()
            outputs[candidate.owner] = list(segments)
            for index, segment in enumerate(segments):
                if segment.strip():
                    pending.append(_Segment(candidate.owner, index, segment))

        retry_segments: list[_Segment] = []
        for start in range(0, len(pending), self._batch_size):
            segment_batch = pending[start : start + self._batch_size]
            chunk_retries = segment_batch
            try:
                batch = self._engine.translate_many(
                    [segment.text for segment in segment_batch]
                )
            except Exception:
                pass
            else:
                if isinstance(batch, list) and len(batch) == len(segment_batch):
                    chunk_retries = []
                    for value, segment in zip(batch, segment_batch, strict=True):
                        try:
                            outputs[segment.owner][segment.index] = (
                                self._validate_segment(value)
                            )
                            completed.add((segment.owner, segment.index))
                        except Exception:
                            chunk_retries.append(segment)

            retry_segments.extend(chunk_retries)

        for segment in retry_segments:
            try:
                result = self._engine.translate_many([segment.text])
                if not isinstance(result, list) or len(result) != 1:
                    raise ValueError("invalid translation result")
                outputs[segment.owner][segment.index] = self._validate_segment(
                    result[0]
                )
            except Exception:
                continue
            completed.add((segment.owner, segment.index))

        for candidate in candidates:
            required = {
                (candidate.owner, index)
                for index, segment in enumerate(candidate.protected.translatable_segments())
                if segment.strip()
            }
            if not required.issubset(completed):
                continue
            try:
                successes[candidate.owner] = candidate.protected.restore_segments(
                    outputs[candidate.owner]
                )
            except Exception:
                continue
        return successes

    @staticmethod
    def _validate_segment(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid translation result")
        if _MARKER_PATTERN.search(value):
            raise ValueError(_RESTORE_ERROR)
        return value.strip()

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
    return hashlib.sha256(canonical_source_text(text).encode("utf-8")).hexdigest()
