"""本地语音转写（faster-whisper，可选依赖，惰性加载）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Callable

from ..bili.client import BiliError
from ..constants import ErrorCode
from ..models import Segment

_EXTRA_PATH_DONE: set[str] = set()


class Transcriber:
    """把音频文件转成与字幕等价的 Segment 列表。"""

    _models: dict[tuple[str, str, str], Any] = {}

    def __init__(
        self,
        settings_getter: Callable[[], Any] | None = None,
        *,
        logger: Any = None,
    ) -> None:
        self._settings = settings_getter or (lambda: None)
        self.logger = logger

    def _opt(self, name: str, default: Any) -> Any:
        settings = self._settings()
        return getattr(settings, name, default) if settings is not None else default

    async def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "",
        timeout: float | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[list[Segment], str]:
        """返回 (segments, 语言)。缺依赖抛 ASR_UNAVAILABLE，失败抛 TRANSCRIBE_FAILED。"""
        if progress:
            progress("loading_model")
        model = await asyncio.to_thread(self._load_model)
        limit = float(timeout or self._opt("transcribe_timeout", 1800) or 1800)
        if progress:
            progress("transcribing")
        try:
            raw, lang = await asyncio.wait_for(
                asyncio.to_thread(self._run, model, audio_path, language),
                timeout=limit,
            )
        except asyncio.TimeoutError as exc:
            raise BiliError(
                ErrorCode.TRANSCRIBE_FAILED, f"转写超过 {int(limit)} 秒未完成"
            ) from exc
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            hint = ""
            if "metadata_errors" in str(exc):
                hint = "（av 版本不兼容，请 pip install \"av<16\"）"
            raise BiliError(ErrorCode.TRANSCRIBE_FAILED, f"转写失败{hint}：{exc}") from exc
        segments: list[Segment] = []
        for start, end, text in raw:
            text = (text or "").strip()
            if not text:
                continue
            start_ms = max(0, int(round(float(start) * 1000)))
            end_ms = max(start_ms, int(round(float(end) * 1000)))
            segments.append(
                Segment(index=len(segments), start_ms=start_ms, end_ms=end_ms, text=text)
            )
        if not segments:
            raise BiliError(ErrorCode.TRANSCRIBE_FAILED, "转写没有产出任何文本")
        return segments, (lang or "").strip()

    def _load_model(self) -> Any:
        extra = str(self._opt("asr_deps_path", "") or "").strip()
        if extra:
            path = str(Path(extra).expanduser())
            if path not in _EXTRA_PATH_DONE and Path(path).is_dir():
                _EXTRA_PATH_DONE.add(path)
                sys.path.insert(0, path)
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001
            raise BiliError(
                ErrorCode.ASR_UNAVAILABLE,
                "未安装 faster-whisper，无法本地转写；可 pip install faster-whisper "
                "或在设置里填写 asr_deps_path 指向已安装的目录",
            ) from exc

        name = str(self._opt("whisper_model", "small") or "small")
        device = str(self._opt("whisper_device", "cpu") or "cpu")
        compute = str(self._opt("whisper_compute_type", "int8") or "int8")
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        if device == "cuda" and compute == "int8":
            compute = "float16"
        key = (name, device, compute)
        cached = Transcriber._models.get(key)
        if cached is None:
            cached = WhisperModel(name, device=device, compute_type=compute)
            Transcriber._models[key] = cached
        return cached

    def _run(
        self, model: Any, audio_path: str, language: str
    ) -> tuple[list[tuple[float, float, str]], str]:
        segments, info = model.transcribe(
            audio_path,
            language=language or None,
            beam_size=5,
            best_of=5,
            temperature=[0.0, 0.2, 0.4],
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 900, "speech_pad_ms": 300},
            no_speech_threshold=0.7,
            compression_ratio_threshold=2.3,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )
        rows: list[tuple[float, float, str]] = []
        for item in segments:
            rows.append((float(item.start or 0), float(item.end or 0), item.text or ""))
        lang = str(getattr(info, "language", "") or "")
        return rows, lang


def _cuda_available() -> bool:
    try:
        import torch  # type: ignore[import-not-found]

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False
