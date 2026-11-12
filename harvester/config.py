"""配置读取：把 AstrBot 传入的 dict 收敛成强类型 Settings。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from .constants import DEFAULT_UA


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _as_float(value: Any, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


@dataclass(slots=True)
class Settings:
    provider_id: str = ""
    json_mode: bool = True
    temperature: float = 0.2
    max_tokens: int = 4096
    llm_timeout: int = 60
    chunk_chars: int = 6000
    max_chunks: int = 8
    request_interval: float = 1.0
    collection_page_size: int = 30
    max_collection_items: int = 500
    queue_concurrency: int = 1
    raw_retention_days: int = 30
    review_required: bool = True
    memewiki_enabled: bool = True
    memewiki_min_confidence: float = 0.6
    auto_parse_on_link: bool = False
    inject_enabled: bool = True
    inject_max_chars: int = 600
    user_agent: str = DEFAULT_UA

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "Settings":
        raw = dict(config or {})
        return cls(
            provider_id=str(raw.get("provider_id") or "").strip(),
            json_mode=_as_bool(raw.get("json_mode"), True),
            temperature=_as_float(raw.get("temperature"), 0.2, minimum=0.0, maximum=2.0),
            max_tokens=_as_int(raw.get("max_tokens"), 4096, minimum=256, maximum=65536),
            llm_timeout=_as_int(raw.get("llm_timeout"), 60, minimum=5, maximum=600),
            chunk_chars=_as_int(raw.get("chunk_chars"), 6000, minimum=800, maximum=32000),
            max_chunks=_as_int(raw.get("max_chunks"), 8, minimum=1, maximum=40),
            request_interval=_as_float(raw.get("request_interval"), 1.0, minimum=0.2, maximum=30.0),
            collection_page_size=_as_int(raw.get("collection_page_size"), 30, minimum=1, maximum=100),
            max_collection_items=_as_int(raw.get("max_collection_items"), 500, minimum=1, maximum=5000),
            queue_concurrency=_as_int(raw.get("queue_concurrency"), 1, minimum=1, maximum=4),
            raw_retention_days=_as_int(raw.get("raw_retention_days"), 30, minimum=0, maximum=3650),
            review_required=_as_bool(raw.get("review_required"), True),
            memewiki_enabled=_as_bool(raw.get("memewiki_enabled"), True),
            memewiki_min_confidence=_as_float(raw.get("memewiki_min_confidence"), 0.6, minimum=0.0, maximum=1.0),
            auto_parse_on_link=_as_bool(raw.get("auto_parse_on_link"), False),
            inject_enabled=_as_bool(raw.get("inject_enabled"), True),
            inject_max_chars=_as_int(raw.get("inject_max_chars"), 600, minimum=0, maximum=4000),
            user_agent=str(raw.get("user_agent") or DEFAULT_UA).strip() or DEFAULT_UA,
        )

    def merge(self, overrides: dict[str, Any]) -> "Settings":
        """用 WebUI 保存的运行时设置覆盖默认值。"""
        data = asdict(self)
        allowed = {f.name for f in fields(self)}
        for key, value in (overrides or {}).items():
            if key in allowed and value is not None:
                data[key] = value
        return Settings.from_config(data)

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.queue_concurrency > 1 and self.request_interval < 1.0:
            problems.append("并发大于 1 时建议把请求间隔提高到 1 秒以上，否则容易触发风控。")
        if self.max_chunks * self.chunk_chars > 320_000:
            problems.append("单视频最大抽取文本量过大，可能显著增加模型费用。")
        return problems
