"""字幕选轨与解析。"""

from __future__ import annotations

import hashlib
from typing import Any

from ..models import Segment, Track

ZH_LANS = {"zh", "zh-cn", "zh-hans", "zh-hant", "zh-tw", "zh-hk"}
AI_ZH_LANS = {"ai-zh", "ai-hans", "ai-hant", "ai-zh-cn"}

#: 轨道优先级：中文 CC → 中文 AI → 其它
_CC_SCORE = 0
_AI_ZH_SCORE = 10
_AI_OTHER_SCORE = 20
_OTHER_CC_SCORE = 30
_OTHER_SCORE = 40


def is_chinese(lan: str) -> bool:
    value = (lan or "").lower()
    return value in ZH_LANS or value in AI_ZH_LANS or value.startswith("zh-")


def _score(track: Track) -> int:
    lan = (track.lan or "").lower()
    if not track.is_ai and is_chinese(lan):
        return _CC_SCORE
    if track.is_ai and lan in AI_ZH_LANS:
        return _AI_ZH_SCORE
    if track.is_ai and is_chinese(lan):
        return _AI_ZH_SCORE
    if is_chinese(lan):
        return _AI_ZH_SCORE + 1 if not track.is_ai else _AI_ZH_SCORE
    if not track.is_ai:
        return _OTHER_CC_SCORE
    return _OTHER_SCORE


def rank_tracks(tracks: list[Track]) -> list[Track]:
    return sorted(tracks, key=lambda t: (_score(t), 0 if t.url else 1))


def pick_track(tracks: list[Track]) -> Track | None:
    ranked = rank_tracks(tracks)
    return ranked[0] if ranked else None


def track_kind(track: Track | None) -> str:
    if track is None:
        return ""
    if track.is_ai:
        return "ai"
    return "cc"


def provider_label(track: Track | None, endpoint: str, resolved_ai: bool = False) -> str:
    """写入 transcripts.provider 的取值。"""
    if track is None:
        return endpoint or "unknown"
    if resolved_ai:
        return "ai_stat"
    if endpoint == "dm":
        return "dm"
    return "cc" if not track.is_ai else "ai"


def _ms(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number <= 0:
        return 0
    # B 站字幕用秒（浮点）；防御性处理毫秒
    return int(round(number * 1000)) if number < 100_000 else int(number)


def parse_subtitle_payload(payload: Any) -> list[Segment]:
    if not isinstance(payload, dict):
        return []
    body = payload.get("body")
    if not isinstance(body, list):
        return []
    segments: list[Segment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").replace("\n", " ").strip()
        if not text:
            continue
        start = _ms(item.get("from"))
        end = _ms(item.get("to"))
        if end < start:
            end = start
        segments.append(Segment(index=len(segments), start_ms=start, end_ms=end, text=text))
    return segments


def segments_text_hash(segments: list[Segment]) -> str:
    plain = "".join(seg.text for seg in segments)
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def merge_segments(*groups: list[Segment]) -> list[Segment]:
    """按时间轴合并多段字幕并重新编号（用于分块结果回填）。"""
    combined = [seg for group in groups for seg in group]
    combined.sort(key=lambda s: (s.start_ms, s.end_ms))
    return [Segment(index=i, start_ms=s.start_ms, end_ms=s.end_ms, text=s.text) for i, s in enumerate(combined)]
