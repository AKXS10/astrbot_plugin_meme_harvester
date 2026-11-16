"""媒体解析测试：yt-dlp 探测、字幕优先、失败回退转写、错误映射。"""

from __future__ import annotations

import unittest
from pathlib import Path

from harvester.bili.client import BiliError
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.models import Segment
from harvester.sources.media import MediaProbe, MediaResolver, _translate_error

VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:03.000\n今天讲一个梗叫做尊嘟假嘟\n"

BASE_INFO = {
    "id": "abc123",
    "title": "测试视频",
    "uploader": "UP 主",
    "duration": 120,
    "extractor_key": "Youtube",
    "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "subtitles": {"zh-Hans": [{"ext": "vtt", "url": "https://sub.example/zh.vtt"}]},
    "automatic_captions": {"en": [{"ext": "vtt", "url": "https://sub.example/en.vtt"}]},
}


class FakeTranscriber:
    def __init__(self, segments: list[Segment] | None = None, error: Exception | None = None) -> None:
        self.segments = segments or [Segment(index=0, start_ms=0, end_ms=900, text="转写文本")]
        self.error = error
        self.calls: list[str] = []

    async def transcribe(self, audio_path, *, language="", timeout=None, progress=None):
        self.calls.append(str(audio_path))
        if self.error is not None:
            raise self.error
        return self.segments, "zh"


class ProbeMedia(MediaResolver):
    def __init__(self, info: dict, settings: Settings | None = None) -> None:
        super().__init__(settings_getter=lambda: settings or Settings())
        self.info = info

    def _probe_sync(self, url: str) -> dict:
        return self.info


class ResolveMedia(MediaResolver):
    def __init__(
        self,
        probe: MediaProbe,
        *,
        text: str | None = VTT,
        transcriber: FakeTranscriber | None = None,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(
            settings_getter=lambda: settings or Settings(),
            transcriber=transcriber or FakeTranscriber(),  # type: ignore[arg-type]
        )
        self.probe_result = probe
        self.text = text

    async def probe(self, url: str) -> MediaProbe:  # type: ignore[override]
        return self.probe_result

    async def _download_text(self, url: str) -> str:
        if self.text is None:
            raise BiliError(ErrorCode.DOWNLOAD_FAILED, "HTTP 404")
        return self.text

    async def _download_audio(self, url: str, tmp: Path) -> Path:
        path = Path(tmp) / "audio.m4a"
        path.write_bytes(b"fake")
        return path


def probe(**overrides) -> MediaProbe:
    data = {
        "platform": "youtube",
        "source_id": "youtube:abc123",
        "url": "https://www.youtube.com/watch?v=abc123",
        "title": "测试视频",
        "uploader": "UP 主",
        "duration": 120,
        "subtitle_url": "https://sub.example/zh.vtt",
        "subtitle_ext": "vtt",
        "subtitle_lang": "zh-Hans",
    }
    data.update(overrides)
    return MediaProbe(**data)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_manual_subtitle(self) -> None:
        result = await ProbeMedia(BASE_INFO).probe("https://youtu.be/abc123")
        self.assertEqual(result.platform, "youtube")
        self.assertEqual(result.source_id, "youtube:abc123")
        self.assertEqual(result.subtitle_lang, "zh-Hans")
        self.assertFalse(result.subtitle_is_auto)
        self.assertTrue(result.subtitle_url.endswith("zh.vtt"))

    async def test_uses_auto_captions(self) -> None:
        info = dict(BASE_INFO, subtitles={})
        result = await ProbeMedia(info).probe("https://youtu.be/abc123")
        self.assertEqual(result.subtitle_lang, "en")
        self.assertTrue(result.subtitle_is_auto)

    async def test_duration_cap(self) -> None:
        info = dict(BASE_INFO, duration=7200)
        with self.assertRaises(BiliError) as ctx:
            await ProbeMedia(info).probe("https://youtu.be/abc123")
        self.assertEqual(ctx.exception.code, ErrorCode.MEDIA_UNSUPPORTED)

    async def test_live_rejected(self) -> None:
        info = dict(BASE_INFO, is_live=True)
        with self.assertRaises(BiliError) as ctx:
            await ProbeMedia(info).probe("https://youtu.be/abc123")
        self.assertEqual(ctx.exception.code, ErrorCode.MEDIA_UNSUPPORTED)


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_subtitle_path(self) -> None:
        resolver = ResolveMedia(probe())
        outcome = await resolver.resolve("https://youtu.be/abc123", Path("."))
        self.assertEqual(outcome.provider, "yt_sub")
        self.assertEqual(outcome.lang, "zh-Hans")
        self.assertTrue(outcome.segments)

    async def test_fallback_to_whisper(self) -> None:
        fake = FakeTranscriber([Segment(0, 0, 900, "本地转写出来的梗")])
        resolver = ResolveMedia(probe(subtitle_url=""), transcriber=fake)
        outcome = await resolver.resolve("https://youtu.be/abc123", Path("."))
        self.assertEqual(outcome.provider, "whisper")
        self.assertEqual(outcome.segments[0].text, "本地转写出来的梗")
        self.assertEqual(len(fake.calls), 1)

    async def test_subtitle_download_fail_and_transcribe_off(self) -> None:
        settings = Settings(transcribe_enabled=False)
        resolver = ResolveMedia(probe(), text=None, settings=settings)
        with self.assertRaises(BiliError) as ctx:
            await resolver.resolve("https://youtu.be/abc123", Path("."))
        self.assertEqual(ctx.exception.code, ErrorCode.DOWNLOAD_FAILED)

    async def test_transcribe_unavailable(self) -> None:
        fake = FakeTranscriber(error=BiliError(ErrorCode.ASR_UNAVAILABLE, "缺依赖"))
        resolver = ResolveMedia(probe(subtitle_url=""), transcriber=fake)
        with self.assertRaises(BiliError) as ctx:
            await resolver.resolve("https://youtu.be/abc123", Path("."))
        self.assertEqual(ctx.exception.code, ErrorCode.ASR_UNAVAILABLE)


class ErrorMapTests(unittest.TestCase):
    def test_unsupported_url(self) -> None:
        error = _translate_error(Exception("Unsupported URL: https://example.com"))
        self.assertEqual(error.code, ErrorCode.MEDIA_UNSUPPORTED)

    def test_login_required(self) -> None:
        error = _translate_error(Exception("ERROR: Fresh cookies needed for this request"))
        self.assertEqual(error.code, ErrorCode.MEDIA_UNSUPPORTED)
        self.assertIn("Cookie", str(error))

    def test_generic(self) -> None:
        error = _translate_error(Exception("network unreachable"))
        self.assertEqual(error.code, ErrorCode.DOWNLOAD_FAILED)
