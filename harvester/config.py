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
    risk_cooldown: float = 1800.0
    collection_page_size: int = 30
    max_collection_items: int = 500
    queue_concurrency: int = 1
    raw_retention_days: int = 30
    temp_retention_days: int = 3
    review_required: bool = True
    memewiki_enabled: bool = True
    memewiki_min_confidence: float = 0.6
    auto_parse_on_link: bool = False
    inject_enabled: bool = True
    inject_max_chars: int = 600
    user_agent: str = DEFAULT_UA
    # ---- 多平台视频（字幕优先，无字幕再本地转写） ----
    media_enabled: bool = True
    transcribe_enabled: bool = True
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    transcribe_language: str = ""
    asr_deps_path: str = ""
    media_max_duration: int = 5400
    transcribe_timeout: int = 1800
    download_timeout: int = 600
    ytdlp_cookies_file: str = ""
    ytdlp_proxy: str = ""
    keep_temp_files: bool = False
    # ---- 网页 / Wiki 抓取 ----
    web_enabled: bool = True
    web_timeout: int = 20
    web_max_bytes: int = 2_000_000
    web_min_chars: int = 200
    web_cookies: str = ""
    max_source_chars: int = 60_000

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
            risk_cooldown=_as_float(raw.get("risk_cooldown"), 1800.0, minimum=60.0, maximum=86400.0),
            collection_page_size=_as_int(raw.get("collection_page_size"), 30, minimum=1, maximum=100),
            max_collection_items=_as_int(raw.get("max_collection_items"), 500, minimum=1, maximum=5000),
            queue_concurrency=_as_int(raw.get("queue_concurrency"), 1, minimum=1, maximum=4),
            raw_retention_days=_as_int(raw.get("raw_retention_days"), 30, minimum=0, maximum=3650),
            temp_retention_days=_as_int(raw.get("temp_retention_days"), 3, minimum=1, maximum=90),
            review_required=_as_bool(raw.get("review_required"), True),
            memewiki_enabled=_as_bool(raw.get("memewiki_enabled"), True),
            memewiki_min_confidence=_as_float(raw.get("memewiki_min_confidence"), 0.6, minimum=0.0, maximum=1.0),
            auto_parse_on_link=_as_bool(raw.get("auto_parse_on_link"), False),
            inject_enabled=_as_bool(raw.get("inject_enabled"), True),
            inject_max_chars=_as_int(raw.get("inject_max_chars"), 600, minimum=0, maximum=4000),
            user_agent=str(raw.get("user_agent") or DEFAULT_UA).strip() or DEFAULT_UA,
            media_enabled=_as_bool(raw.get("media_enabled"), True),
            transcribe_enabled=_as_bool(raw.get("transcribe_enabled"), True),
            whisper_model=_whisper_model(raw.get("whisper_model")),
            whisper_device=_choice(raw.get("whisper_device"), ("cpu", "cuda", "auto"), "cpu"),
            whisper_compute_type=_choice(
                raw.get("whisper_compute_type"),
                ("int8", "int8_float16", "float16", "float32"),
                "int8",
            ),
            transcribe_language=str(raw.get("transcribe_language") or "").strip(),
            asr_deps_path=str(raw.get("asr_deps_path") or "").strip(),
            media_max_duration=_as_int(
                raw.get("media_max_duration"), 5400, minimum=30, maximum=43_200
            ),
            transcribe_timeout=_as_int(
                raw.get("transcribe_timeout"), 1800, minimum=60, maximum=14_400
            ),
            download_timeout=_as_int(
                raw.get("download_timeout"), 600, minimum=30, maximum=3600
            ),
            ytdlp_cookies_file=str(raw.get("ytdlp_cookies_file") or "").strip(),
            ytdlp_proxy=str(raw.get("ytdlp_proxy") or "").strip(),
            keep_temp_files=_as_bool(raw.get("keep_temp_files"), False),
            web_enabled=_as_bool(raw.get("web_enabled"), True),
            web_timeout=_as_int(raw.get("web_timeout"), 20, minimum=3, maximum=120),
            web_max_bytes=_as_int(
                raw.get("web_max_bytes"), 2_000_000, minimum=50_000, maximum=20_000_000
            ),
            web_min_chars=_as_int(raw.get("web_min_chars"), 200, minimum=0, maximum=10_000),
            web_cookies=str(raw.get("web_cookies") or "").strip(),
            max_source_chars=_as_int(
                raw.get("max_source_chars"), 60_000, minimum=2_000, maximum=500_000
            ),
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
        if self.transcribe_enabled and self.whisper_model in ("medium", "large-v3") and self.whisper_device == "cpu":
            problems.append("在 CPU 上用 medium/large 模型转写会非常慢，建议改用 small 或提供 GPU。")
        if self.media_max_duration > 7200:
            problems.append("单条媒体时长上限超过 2 小时，本地转写可能需要很久。")
        return problems


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _whisper_model(value: Any) -> str:
    text = str(value or "").strip() or "small"
    allowed = {"tiny", "base", "small", "medium", "large-v3", "large"}
    return text if text in allowed else "small"
