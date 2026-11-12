"""轻量数据模型：全部为 dataclass，便于测试与序列化。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Segment:
    """一条字幕。时间单位统一为毫秒。"""

    index: int
    start_ms: int
    end_ms: int
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "i": self.index,
            "s": self.start_ms,
            "e": self.end_ms,
            "t": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        return cls(
            index=int(data.get("i", 0)),
            start_ms=int(data.get("s", 0)),
            end_ms=int(data.get("e", 0)),
            text=str(data.get("t", "")),
        )


@dataclass(slots=True)
class Track:
    """一个字幕轨。"""

    lan: str
    label: str = ""
    url: str = ""
    ai_status: int = 0
    is_ai: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Track":
        lan = str(data.get("lan") or "")
        ai_status = _to_int(data.get("ai_status"), 0)
        return cls(
            lan=lan,
            label=str(data.get("lan_doc") or data.get("label") or ""),
            url=str(data.get("subtitle_url") or ""),
            ai_status=ai_status,
            is_ai=lan.startswith("ai-") or bool(data.get("is_ai")),
        )


@dataclass(slots=True)
class VideoInfo:
    bvid: str
    aid: int = 0
    cid: int = 0
    title: str = ""
    owner_name: str = ""
    owner_mid: int = 0
    pubdate: int = 0
    duration: int = 0
    season_id: int = 0
    page_no: int = 0
    is_charge_blocked: bool = False
    pages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def video_key(self) -> str:
        """多 P 视频按 P 区分，避免同一 bvid 互相覆盖。"""
        if self.page_no > 1:
            return f"{self.bvid}_p{self.page_no}"
        return self.bvid

    def to_dict(self) -> dict[str, Any]:
        return {
            "bvid": self.bvid,
            "aid": self.aid,
            "cid": self.cid,
            "title": self.title,
            "owner_name": self.owner_name,
            "owner_mid": self.owner_mid,
            "pubdate": self.pubdate,
            "duration": self.duration,
            "season_id": self.season_id,
            "page_no": self.page_no,
            "is_charge_blocked": self.is_charge_blocked,
        }


@dataclass(slots=True)
class MemeCandidate:
    """LLM 抽取出的候选词条。"""

    term: str
    definition: str = ""
    usage: str = ""
    quote: str = ""
    start_ms: int = 0
    confidence: float = 0.5
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "definition": self.definition,
            "usage": self.usage,
            "quote": self.quote,
            "start_ms": self.start_ms,
            "confidence": self.confidence,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
        }


@dataclass(slots=True)
class UpsertResult:
    meme_id: int
    created: bool
    filled: list[str] = field(default_factory=list)
    conflict: str = ""


@dataclass(slots=True)
class TaskRecord:
    id: str
    kind: str
    target: str
    status: str
    progress: int = 0
    attempts: int = 0
    last_error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "status": self.status,
            "progress": self.progress,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "payload": self.payload,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class SyncResult:
    scanned: int = 0
    synced: int = 0
    existed: int = 0
    skipped: int = 0
    failed: int = 0
    mode: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "synced": self.synced,
            "existed": self.existed,
            "skipped": self.skipped,
            "failed": self.failed,
            "mode": self.mode,
            "message": self.message,
        }


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
