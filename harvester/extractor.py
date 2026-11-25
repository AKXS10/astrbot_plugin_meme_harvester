"""两阶段抽取：关键词快筛 → LLM 精抽 → 校验/去重。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .constants import (
    CANDIDATE_KEYWORDS,
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_USER_TEMPLATE,
)
from .llm import LLMError, parse_llm_json
from .models import MemeCandidate, Segment
from .text import clean_text, is_meaningful_term, normalize_term, quote_in_source, segments_to_lines, truncate

CompleteFn = Callable[[str, str], Awaitable[str]]


@dataclass(slots=True)
class ExtractOutcome:
    candidates: list[MemeCandidate] = field(default_factory=list)
    chunks_sent: int = 0
    chunks_total: int = 0
    raw_count: int = 0
    dropped: int = 0
    used_fallback: bool = False
    failed_chunks: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidates": [c.to_dict() for c in self.candidates],
            "chunks_sent": self.chunks_sent,
            "chunks_total": self.chunks_total,
            "raw_count": self.raw_count,
            "dropped": self.dropped,
            "used_fallback": self.used_fallback,
            "failed_chunks": self.failed_chunks,
        }


class MemeExtractor:
    def __init__(
        self,
        complete: CompleteFn,
        *,
        max_memes: int = 8,
        max_chunks: int = 8,
    ) -> None:
        self._complete = complete
        self.max_memes = max(1, int(max_memes))
        self.max_chunks = max(1, int(max_chunks))

    async def extract(
        self,
        title: str,
        owner: str,
        chunks: list[list[Segment]],
        *,
        plain_text: str = "",
    ) -> ExtractOutcome:
        outcome = ExtractOutcome(chunks_total=len(chunks))
        if not chunks:
            return outcome

        selected, used_fallback = self._select_chunks(chunks)
        outcome.chunks_sent = len(selected)
        outcome.used_fallback = used_fallback

        merged: dict[str, MemeCandidate] = {}
        for chunk in selected:
            try:
                raw = await self._complete(
                    EXTRACT_SYSTEM_PROMPT,
                    EXTRACT_USER_TEMPLATE.format(
                        title=title or "（未知）",
                        owner=owner or "（未知）",
                        lines=segments_to_lines(chunk),
                    ),
                )
            except LLMError as exc:
                outcome.failed_chunks += 1
                outcome.errors.append(exc.message)
                continue

            payload = parse_llm_json(raw)
            if payload is None:
                outcome.dropped += 1
                outcome.errors.append("模型输出不是合法 JSON")
                continue
            items = payload.get("memes")
            if not isinstance(items, list):
                continue
            outcome.raw_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    outcome.dropped += 1
                    continue
                candidate = self._validate(item, chunk, plain_text)
                if candidate is None:
                    outcome.dropped += 1
                    continue
                key = normalize_term(candidate.term)
                existing = merged.get(key)
                if existing is None:
                    merged[key] = candidate
                else:
                    merged[key] = _merge_candidates(existing, candidate)

        ranked = sorted(merged.values(), key=lambda c: c.confidence, reverse=True)[: self.max_memes]
        outcome.candidates = ranked
        return outcome

    @staticmethod
    def _select_chunks(chunks: list[list[Segment]]) -> tuple[list[list[Segment]], bool]:
        signal: list[list[Segment]] = []
        for chunk in chunks:
            if any(_has_keyword(seg.text) for seg in chunk):
                signal.append(chunk)
        if signal:
            return signal[:8], False
        # 全程没有关键词：只抽最靠前的 2 块，尽量低成本兜底
        return chunks[:2], True

    @staticmethod
    def _validate(item: dict[str, Any], chunk: list[Segment], plain_text: str) -> MemeCandidate | None:
        term = clean_text(str(item.get("term") or ""))
        if not is_meaningful_term(term):
            return None
        definition = truncate(clean_text(str(item.get("definition") or "")), 300)
        if not definition:
            return None

        quote = clean_text(str(item.get("quote") or ""))
        start_ms = _to_int(item.get("start_ms"), 0)
        chunk_plain = plain_text or "".join(seg.text for seg in chunk)

        if quote and not quote_in_source(quote, chunk_plain):
            quote = ""
        if not quote:
            matched = _find_segment(term, chunk)
            if matched is None:
                return None  # 字幕里找不到依据，判定为幻觉
            quote = matched.text
            start_ms = matched.start_ms
        else:
            matched = _find_segment_by_text(quote, chunk)
            if matched is not None:
                start_ms = matched.start_ms

        confidence = _to_float(item.get("confidence"), 0.5)
        confidence = max(0.0, min(1.0, confidence))
        aliases = _string_list(item.get("aliases"), limit=5)
        tags = _string_list(item.get("tags"), limit=5)
        usage = truncate(clean_text(str(item.get("usage") or "")), 300)
        return MemeCandidate(
            term=term,
            definition=definition,
            usage=usage,
            quote=quote,
            start_ms=start_ms,
            confidence=confidence,
            aliases=aliases,
            tags=tags,
        )


def _merge_candidates(a: MemeCandidate, b: MemeCandidate) -> MemeCandidate:
    primary, secondary = (a, b) if a.confidence >= b.confidence else (b, a)
    return MemeCandidate(
        term=primary.term,
        definition=primary.definition if len(primary.definition) >= len(secondary.definition) else secondary.definition,
        usage=primary.usage or secondary.usage,
        quote=primary.quote,
        start_ms=primary.start_ms,
        confidence=primary.confidence,
        aliases=list(dict.fromkeys([*primary.aliases, *secondary.aliases]))[:5],
        tags=list(dict.fromkeys([*primary.tags, *secondary.tags]))[:5],
    )


def _has_keyword(text: str) -> bool:
    return any(keyword in text for keyword in CANDIDATE_KEYWORDS)


def _find_segment(term: str, chunk: list[Segment]) -> Segment | None:
    norm = normalize_term(term)
    for seg in chunk:
        if norm and norm in normalize_term(seg.text):
            return seg
    return None


def _find_segment_by_text(quote: str, chunk: list[Segment]) -> Segment | None:
    compact = "".join(quote.split())
    for seg in chunk:
        if compact and compact in "".join(seg.text.split()):
            return seg
    return None


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = clean_text(str(item))
        if 1 <= len(text) <= 24 and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = ["ExtractOutcome", "MemeExtractor", "ErrorCode"]
