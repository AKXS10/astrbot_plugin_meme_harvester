"""VTT / SRT 字幕解析（纯函数，便于单测）。"""

from __future__ import annotations

import html
import json
import re
from typing import Iterable

from ..models import Segment

_TS = re.compile(r"^(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?$")
_TAG = re.compile(r"<[^>]{0,200}>")
_ASS = re.compile(r"\{\\[^}]{0,200}\}")
_BLOCK_SPLIT = re.compile(r"\n{2,}")
_VTT_HEADER = re.compile(r"^WEBVTT[^\n]*\n?", re.IGNORECASE)


def parse_time_ms(value: str) -> int:
    """把 ``HH:MM:SS.mmm`` / ``MM:SS.mmm`` / ``MM:SS,mmm`` 转成毫秒。"""
    text = (value or "").strip()
    match = _TS.match(text)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    frac = match.group(4) or "0"
    millis = int((frac + "000")[:3]) if len(frac) < 3 else int(frac[:3])
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def clean_cue_text(text: str) -> str:
    """去掉内联标签 / ASS 控制符 / HTML 实体，压平空白。"""
    if not text:
        return ""
    text = _TAG.sub("", text)
    text = _ASS.sub("", text)
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_subtitle_text(text: str, fmt: str = "") -> list[Segment]:
    """按格式解析；未指定时根据内容猜测。"""
    content = (text or "").lstrip("\ufeff")
    lowered = (fmt or "").lower().strip(".")
    if lowered in ("vtt", "webvtt") or content.lstrip().upper().startswith("WEBVTT"):
        return parse_vtt(content)
    if lowered == "json3" or lowered == "json":
        return parse_json3(content)
    if lowered == "srt" or "-->" in content:
        return parse_srt(content) if lowered == "srt" else _parse_generic(content)
    return []


def parse_vtt(content: str) -> list[Segment]:
    content = _VTT_HEADER.sub("", (content or "").lstrip("\ufeff"))
    return _parse_blocks(content)


def parse_srt(content: str) -> list[Segment]:
    return _parse_blocks((content or "").lstrip("\ufeff"))


def parse_json3(content: str) -> list[Segment]:
    """解析 JSON 字幕：YouTube json3（events/segs）与 B 站 JSON（body/from/to/content）。"""
    try:
        data = json.loads((content or "").strip())
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    raw: list[tuple[int, int, str]] = []

    # YouTube json3: {"events":[{"tStartMs":0,"dDurationMs":1000,"segs":[{"utf8":"..."}]}]}
    events = data.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                start = int(event.get("tStartMs") or 0)
                duration = int(event.get("dDurationMs") or 0)
            except (ValueError, TypeError):
                continue
            segs = event.get("segs")
            if not isinstance(segs, list):
                continue
            body = clean_cue_text("".join(str(s.get("utf8") or "") for s in segs if isinstance(s, dict)))
            if not body:
                continue
            if duration <= 0:
                duration = 1500
            raw.append((start, start + duration, body))

    # B 站 JSON: {"body":[{"from":..,"to":..,"content":".."}]}
    entries = data.get("body")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                start = int(float(entry.get("from") or 0) * 1000)
                end = int(float(entry.get("to") or 0) * 1000)
            except (ValueError, TypeError):
                continue
            body = clean_cue_text(str(entry.get("content") or ""))
            if not body:
                continue
            if end <= start:
                end = start + 1500
            raw.append((start, end, body))

    if not raw:
        return []
    return _dedupe(raw)


def _parse_generic(content: str) -> list[Segment]:
    """VTT 与 SRT 的差别只在时间戳格式，通用解析即可。"""
    return _parse_blocks(content)


def _parse_blocks(content: str) -> list[Segment]:
    raw: list[tuple[int, int, str]] = []
    for block in _BLOCK_SPLIT.split((content or "").strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        timing_idx = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_idx < 0:
            continue
        timing = lines[timing_idx]
        left, _, right = timing.partition("-->")
        start_ms = parse_time_ms(left.strip())
        end_part = right.strip().split(" ")[0]
        end_ms = parse_time_ms(end_part)
        body = clean_cue_text(" ".join(lines[timing_idx + 1 :]))
        if not body:
            continue
        if end_ms <= start_ms:
            end_ms = start_ms + 1500
        raw.append((start_ms, end_ms, body))

    return _dedupe(raw)


def _dedupe(entries: Iterable[tuple[int, int, str]]) -> list[Segment]:
    """去重：完全重复 + YouTube 自动字幕的「滚动追加」中间态。"""
    raw = list(entries)
    seen: set[str] = set()
    unique: list[tuple[int, int, str]] = []
    for start, end, text in raw:
        if len(text) < 2:
            continue
        if text in seen:
            continue
        seen.add(text)
        unique.append((start, end, text))

    kept: list[tuple[int, int, str]] = []
    for i, (start, end, text) in enumerate(unique):
        intermediate = False
        for j in range(i + 1, min(i + 4, len(unique))):
            nxt = unique[j][2]
            if len(nxt) > len(text) and nxt.startswith(text):
                intermediate = True
                break
        if not intermediate:
            kept.append((start, end, text))

    return [
        Segment(index=index, start_ms=start, end_ms=end, text=text)
        for index, (start, end, text) in enumerate(kept)
    ]
