"""文本处理：清洗、归一化、字幕分块。

保持纯函数，方便单元测试，也不依赖 AstrBot。
"""

from __future__ import annotations

import re

from .models import Segment

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)
_SPACE_RE = re.compile(r"\s+")
_EDGE_PUNCT = " \t\r\n\"'“”‘’「」『』《》〈〉()（）[]【】{}<>、，。！？!?.,:：;；~～-—_*#"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[a-zA-Z0-9]")


def clean_text(text: str) -> str:
    """去掉零宽字符、折叠空白。"""
    if not text:
        return ""
    return _SPACE_RE.sub(" ", str(text).translate(_ZERO_WIDTH)).strip()


def normalize_term(term: str) -> str:
    """归一化梗名，用于去重键。

    - 去零宽字符、折叠空白
    - 去掉首尾标点与空白
    - 拉丁字母统一小写（中文不受影响）
    """
    value = clean_text(term).strip(_EDGE_PUNCT)
    value = value.replace(" ", "") if _CJK_RE.search(value) else value.lower()
    return value.strip(_EDGE_PUNCT).lower() if not _CJK_RE.search(value) else value.strip(_EDGE_PUNCT)


def is_meaningful_term(term: str) -> bool:
    """粗筛：2-24 个字符，且至少含一个中文或字母。"""
    value = normalize_term(term)
    if not 2 <= len(value) <= 24:
        return False
    return bool(_CJK_RE.search(value) or _LATIN_RE.search(value))


def segments_to_plain(segments: list[Segment]) -> str:
    return "".join(seg.text for seg in segments)


def segments_to_lines(segments: list[Segment]) -> str:
    return "\n".join(f"{seg.start_ms}|{seg.text}" for seg in segments if seg.text)


def split_chunks(
    segments: list[Segment],
    chunk_chars: int = 6000,
    max_chunks: int = 8,
    overlap_ms: int = 120_000,
) -> list[list[Segment]]:
    """按字数切块，块间按时间轴重叠一段，避免跨块丢词条。

    返回的块数不超过 max_chunks；超出部分由调用方标记 TRUNCATED。
    """
    usable = [s for s in segments if s.text]
    if not usable:
        return []
    limit = max(200, int(chunk_chars))
    # 重叠窗口最多占单块字数的四分之一，避免尾块把下一块撑爆
    overlap_chars = max(0, min(limit // 4, 1200))
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0

    for seg in usable:
        seg_len = len(seg.text)
        if current and size + seg_len > limit:
            chunks.append(current)
            # 从尾部倒序取重叠片段：既要在时间窗内，也不能超过字数预算
            tail: list[Segment] = []
            tail_size = 0
            cutoff = seg.start_ms - overlap_ms
            for prev in reversed(current):
                if prev.start_ms < cutoff:
                    break
                if tail_size + len(prev.text) > overlap_chars:
                    break
                tail.insert(0, prev)
                tail_size += len(prev.text)
            current = tail
            size = tail_size
        current.append(seg)
        size += seg_len

    if current:
        chunks.append(current)
    return chunks[: max(1, int(max_chunks))]


def truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def quote_in_source(quote: str, plain: str) -> bool:
    """抗幻觉校验：quote 必须是字幕原文的子串（忽略空白差异）。"""
    q = _SPACE_RE.sub("", clean_text(quote))
    if not q:
        return False
    return q in _SPACE_RE.sub("", plain)
