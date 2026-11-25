"""非 B 站媒体：yt-dlp 探测平台字幕，缺字幕时下载音频交给本地 ASR。"""

from __future__ import annotations

import asyncio
import glob
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..bili.client import BiliError
from ..bili.http import AiohttpTransport
from ..constants import DEFAULT_UA, ErrorCode
from ..models import Segment
from .asr import Transcriber
from .vtt import parse_subtitle_text

_LANG_PRIORITY = (
    "zh-Hans",
    "zh-Hant",
    "zh",
    "zh-CN",
    "zh-TW",
    "en",
    "en-orig",
    "ja",
    "ko",
    "fr",
    "de",
    "es",
)
_SUB_EXTS = ("vtt", "srt", "ass", "json3")
_AUDIO_EXTS = (".m4a", ".webm", ".mp3", ".ogg", ".opus", ".wav", ".mp4", ".mkv", ".flv")
_PLATFORM_MAP = {
    "youtube": "youtube",
    "bilibili": "bilibili_tv",
    "douyin": "douyin",
    "tiktok": "tiktok",
    "twitter": "x",
    "x": "x",
    "instagram": "instagram",
    "facebook": "facebook",
    "vimeo": "vimeo",
    "youku": "youku",
    "iqiyi": "iqiyi",
    "qq": "tencent",
    "weibo": "weibo",
    "xiaohongshu": "xiaohongshu",
    "soundcloud": "soundcloud",
}


@dataclass(slots=True)
class MediaProbe:
    platform: str
    source_id: str
    url: str
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    subtitle_url: str = ""
    subtitle_ext: str = ""
    subtitle_lang: str = ""
    subtitle_is_auto: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MediaOutcome:
    platform: str
    source_id: str
    url: str
    title: str
    uploader: str
    duration: float
    segments: list[Segment]
    provider: str
    lang: str = ""
    notes: list[str] = field(default_factory=list)


class MediaResolver:
    def __init__(
        self,
        transport: Any = None,
        settings_getter: Callable[[], Any] | None = None,
        *,
        transcriber: Transcriber | None = None,
        logger: Any = None,
    ) -> None:
        self.transport = transport or AiohttpTransport()
        self._settings = settings_getter or (lambda: None)
        self.transcriber = transcriber or Transcriber(settings_getter, logger=logger)
        self.logger = logger

    async def close(self) -> None:
        """关闭底层传输；测试假传输可能没有 close，容错处理。"""
        close = getattr(self.transport, "close", None)
        if close is not None:
            await close()

    def _opt(self, name: str, default: Any) -> Any:
        settings = self._settings()
        return getattr(settings, name, default) if settings is not None else default

    async def resolve(
        self,
        url: str,
        tmp_dir: str | Path,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> MediaOutcome:
        tmp = Path(tmp_dir)
        tmp.mkdir(parents=True, exist_ok=True)
        probe = await self.probe(url)
        notes = list(probe.notes)

        segments: list[Segment] = []
        provider = ""
        lang = ""
        if probe.subtitle_url:
            if progress:
                progress("fetching_subtitle")
            try:
                text = await self._download_text(probe.subtitle_url)
                segments = parse_subtitle_text(text, probe.subtitle_ext)
                if segments:
                    provider = "yt_sub"
                    lang = probe.subtitle_lang
                else:
                    notes.append("平台字幕为空")
            except BiliError as exc:
                notes.append(f"平台字幕下载失败：{exc}")
                segments = []

        if not segments:
            if not self._opt("transcribe_enabled", True):
                if probe.subtitle_url:
                    raise BiliError(ErrorCode.DOWNLOAD_FAILED, "平台字幕下载失败且未启用本地转写")
                raise BiliError(
                    ErrorCode.ASR_UNAVAILABLE,
                    "该链接没有可用字幕，且本地转写未启用",
                )
            if progress:
                progress("downloading_audio")
            audio = await self._download_audio(probe.url, tmp)
            if progress:
                progress("transcribing")
            segments, lang = await self.transcriber.transcribe(
                str(audio),
                language=str(self._opt("transcribe_language", "") or ""),
                timeout=float(self._opt("transcribe_timeout", 1800) or 1800),
                progress=progress,
            )
            provider = "whisper"

        if not segments:
            raise BiliError(ErrorCode.NO_SUBTITLE, "没有获得任何可用文本")

        return MediaOutcome(
            platform=probe.platform,
            source_id=probe.source_id,
            url=probe.url,
            title=probe.title or probe.source_id,
            uploader=probe.uploader,
            duration=probe.duration,
            segments=segments,
            provider=provider,
            lang=lang,
            notes=notes,
        )

    async def probe(self, url: str) -> MediaProbe:
        if not self._opt("media_enabled", True):
            raise BiliError(ErrorCode.MEDIA_UNSUPPORTED, "媒体解析已在设置中关闭")
        try:
            info = await asyncio.to_thread(self._probe_sync, url)
        except ImportError as exc:
            raise BiliError(
                ErrorCode.MEDIA_UNSUPPORTED, "未安装 yt-dlp，无法解析该链接"
            ) from exc
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _translate_error(exc) from exc
        if not isinstance(info, dict):
            raise BiliError(ErrorCode.MEDIA_UNSUPPORTED, "未能识别该链接的媒体信息")
        if info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise BiliError(ErrorCode.MEDIA_UNSUPPORTED, "播放列表为空")
            info = entries[0]
        if info.get("is_live"):
            raise BiliError(ErrorCode.MEDIA_UNSUPPORTED, "不支持直播流")

        duration = float(info.get("duration") or 0)
        cap = float(self._opt("media_max_duration", 5400) or 0)
        if cap and duration and duration > cap:
            raise BiliError(
                ErrorCode.MEDIA_UNSUPPORTED,
                f"时长 {int(duration)} 秒超过上限 {int(cap)} 秒",
            )

        platform = _platform_of(info)
        source_id = f"{platform}:{info.get('id') or _hash(url)}"
        subtitle_url, subtitle_ext, subtitle_lang, is_auto = _pick_subtitle(info)
        return MediaProbe(
            platform=platform,
            source_id=source_id,
            url=info.get("webpage_url") or url,
            title=str(info.get("title") or "").strip(),
            uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
            duration=duration,
            subtitle_url=subtitle_url,
            subtitle_ext=subtitle_ext,
            subtitle_lang=subtitle_lang,
            subtitle_is_auto=is_auto,
        )

    async def _download_text(self, url: str) -> str:
        timeout = float(self._opt("download_timeout", 600) or 600)
        try:
            resp = await asyncio.wait_for(
                self.transport.get(url, headers={"User-Agent": DEFAULT_UA}), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise BiliError(ErrorCode.DOWNLOAD_FAILED, "字幕下载超时") from exc
        if resp.status >= 400:
            raise BiliError(ErrorCode.DOWNLOAD_FAILED, f"字幕下载失败：HTTP {resp.status}")
        return resp.text

    async def _download_audio(self, url: str, tmp: Path) -> Path:
        try:
            path = await asyncio.to_thread(self._download_sync, url, tmp)
        except ImportError as exc:
            raise BiliError(
                ErrorCode.MEDIA_UNSUPPORTED, "未安装 yt-dlp，无法下载音频"
            ) from exc
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _translate_error(exc) from exc
        if path is None:
            raise BiliError(ErrorCode.DOWNLOAD_FAILED, "音频下载完成但找不到输出文件")
        return path

    def _ydl_base(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "socket_timeout": 30,
        }
        cookies = str(self._opt("ytdlp_cookies_file", "") or "").strip()
        if cookies and os.path.isfile(cookies):
            opts["cookiefile"] = cookies
        proxy = str(self._opt("ytdlp_proxy", "") or "").strip()
        if proxy:
            opts["proxy"] = proxy
        return opts

    def _probe_sync(self, url: str) -> dict[str, Any]:
        import yt_dlp  # type: ignore[import-not-found]

        opts = self._ydl_base()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _download_sync(self, url: str, tmp: Path) -> Path | None:
        import yt_dlp  # type: ignore[import-not-found]

        opts = self._ydl_base()
        opts["format"] = "bestaudio/best"
        opts["outtmpl"] = str(tmp / "audio.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        for ext in _AUDIO_EXTS:
            files = sorted(glob.glob(str(tmp / f"audio.*{ext}")))
            if files:
                return Path(files[0])
        candidates = [p for p in tmp.glob("audio.*") if p.is_file()]
        return candidates[0] if candidates else None


def _platform_of(info: dict[str, Any]) -> str:
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    for needle, platform in _PLATFORM_MAP.items():
        if needle in key:
            return platform
    return re.sub(r"[^a-z0-9_]+", "_", key) or "media"


def _pick_subtitle(info: dict[str, Any]) -> tuple[str, str, str, bool]:
    for data, is_auto in (
        (info.get("subtitles") or {}, False),
        (info.get("automatic_captions") or {}, True),
    ):
        if not isinstance(data, dict) or not data:
            continue
        ordered: list[str] = [lang for lang in _LANG_PRIORITY if lang in data]
        ordered += [lang for lang in data if lang not in ordered and not lang.startswith("live_chat")]
        for lang in ordered:
            entries = data.get(lang) or []
            picked = _pick_entry(entries)
            if picked:
                url, ext = picked
                return url, ext, lang, is_auto
    return "", "", "", False


def _pick_entry(entries: Any) -> tuple[str, str] | None:
    if not isinstance(entries, list):
        return None
    usable = [
        e
        for e in entries
        if isinstance(e, dict) and e.get("url") and str(e.get("ext") or "").lower() in _SUB_EXTS
    ]
    for preferred in ("vtt", "srt", "json3", "ass"):
        for entry in usable:
            if str(entry.get("ext") or "").lower() == preferred:
                return str(entry["url"]), preferred
    return None


def _translate_error(exc: Exception) -> BiliError:
    message = str(exc)
    lowered = message.lower()
    if "unsupported url" in lowered or "drm" in lowered:
        return BiliError(ErrorCode.MEDIA_UNSUPPORTED, "该站点暂不受支持（或受 DRM 保护）")
    if "sign in" in lowered or "login" in lowered or "cookies" in lowered:
        return BiliError(
            ErrorCode.MEDIA_UNSUPPORTED,
            "该平台需要登录 Cookie 才能解析；请在设置中提供 yt-dlp cookies 文件",
        )
    if "private video" in lowered or "unavailable" in lowered or "not available" in lowered:
        return BiliError(ErrorCode.DOWNLOAD_FAILED, "视频不可用或已删除")
    head = message.strip().splitlines()[0][:160] if message.strip() else "未知错误"
    return BiliError(ErrorCode.DOWNLOAD_FAILED, f"下载失败：{head}")


def _hash(url: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:16]
