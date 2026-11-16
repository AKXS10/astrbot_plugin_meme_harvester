"""多平台流水线测试：process_url 分派、媒体 / 网页入库、临时目录清理。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from harvester.bili.client import BiliError
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.models import Segment
from harvester.pipeline import VideoPipeline
from harvester.sources.media import MediaOutcome
from harvester.sources.webpage import PageDocument
from harvester.storage import Storage

from tests.test_pipeline import LLM_JSON, FakeBiliClient, make_extractor, make_info

SEGMENTS = [
    Segment(0, 0, 0, "今天讲一个梗叫做尊嘟假嘟"),
    Segment(1, 0, 0, "尊嘟假嘟就是真的假的的谐音"),
]


class FakeMedia:
    def __init__(self, outcome: MediaOutcome, error: Exception | None = None) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[str] = []

    async def resolve(self, url: str, tmp_dir: Any, *, progress=None) -> MediaOutcome:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        (Path(tmp_dir) / "audio.m4a").write_bytes(b"x")
        return self.outcome


class FakeWeb:
    def __init__(self, doc: PageDocument, error: Exception | None = None) -> None:
        self.doc = doc
        self.error = error

    async def fetch(self, url: str) -> PageDocument:
        if self.error is not None:
            raise self.error
        return self.doc


def media_outcome() -> MediaOutcome:
    return MediaOutcome(
        platform="youtube",
        source_id="youtube:abc123",
        url="https://www.youtube.com/watch?v=abc123",
        title="测试视频",
        uploader="UP 主",
        duration=120,
        segments=list(SEGMENTS),
        provider="yt_sub",
        lang="zh-Hans",
        notes=["平台字幕下载失败，已回退转写"],
    )


def page_doc() -> PageDocument:
    return PageDocument(
        platform="wiki",
        source_id="web:0123456789abcdef",
        url="https://zh.moegirl.org.cn/尊嘟假嘟",
        title="尊嘟假嘟",
        author="",
        text="\n\n".join(s.text for s in SEGMENTS),
        segments=list(SEGMENTS),
        provider="wiki",
        lang="zh",
        via="api",
    )


class PipelineUrlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.storage = Storage(self.root / "harvester.db")
        await self.storage.open()
        self.settings = Settings(
            review_required=False,
            chunk_chars=6000,
            max_chunks=8,
            keep_temp_files=False,
            memewiki_min_confidence=0.6,
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    def build(self, *, media=None, web=None) -> VideoPipeline:
        return VideoPipeline(
            FakeBiliClient(make_info()),
            self.storage,
            make_extractor(LLM_JSON),
            lambda: self.settings,
            media_resolver=media,
            web_resolver=web,
            tmp_root=self.root / "tmp",
        )

    async def test_bilibili_url_goes_to_video_pipeline(self) -> None:
        pipeline = self.build()
        result = await pipeline.process_url("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.provider, "cc")
        self.assertEqual(result.platform, "bilibili")

    async def test_media_url(self) -> None:
        media = FakeMedia(media_outcome())
        pipeline = self.build(media=media)
        result = await pipeline.process_media("https://youtu.be/abc123")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.platform, "youtube")
        self.assertEqual(result.provider, "yt_sub")
        self.assertEqual(result.memes_created, 1)
        self.assertIn("已回退转写", " ".join(result.notes))
        video = await self.storage.get_video("youtube:abc123", platform="youtube")
        self.assertEqual(video["status"], "done")
        transcript = await self.storage.get_transcript(video["id"])
        self.assertEqual(transcript["provider"], "yt_sub")
        self.assertEqual(list((self.root / "tmp").glob("job-*")), [])

    async def test_media_failure_cleans_temp_dir(self) -> None:
        pipeline = self.build(
            media=FakeMedia(media_outcome(), error=BiliError(ErrorCode.ASR_UNAVAILABLE, "缺依赖"))
        )
        with self.assertRaises(BiliError) as ctx:
            await pipeline.process_media("https://youtu.be/abc123")
        self.assertEqual(ctx.exception.code, ErrorCode.ASR_UNAVAILABLE)
        self.assertEqual(list((self.root / "tmp").glob("job-*")), [])

    async def test_page_url(self) -> None:
        web = FakeWeb(page_doc())
        pipeline = self.build(web=web)
        result = await pipeline.process_page("https://zh.moegirl.org.cn/尊嘟假嘟")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.platform, "wiki")
        self.assertEqual(result.provider, "wiki")
        self.assertEqual(result.memes_created, 1)
        video = await self.storage.get_video("web:0123456789abcdef", platform="wiki")
        self.assertEqual(video["status"], "done")

    async def test_process_url_dispatches_media(self) -> None:
        media = FakeMedia(media_outcome())
        pipeline = self.build(media=media, web=None)
        result = await pipeline.process_url("https://youtu.be/abc123")
        self.assertEqual(result.platform, "youtube")
        self.assertEqual(media.calls, ["https://youtu.be/abc123"])

    async def test_process_url_rejects_unsupported(self) -> None:
        pipeline = self.build()
        with self.assertRaises(BiliError) as ctx:
            await pipeline.process_url("ftp://example.com/a")
        self.assertEqual(ctx.exception.code, ErrorCode.BAD_URL)

    async def test_media_disabled(self) -> None:
        pipeline = self.build(media=None)
        with self.assertRaises(BiliError) as ctx:
            await pipeline.process_media("https://youtu.be/abc123")
        self.assertEqual(ctx.exception.code, ErrorCode.MEDIA_UNSUPPORTED)

    async def test_second_run_skips(self) -> None:
        pipeline = self.build(media=FakeMedia(media_outcome()))
        await pipeline.process_media("https://youtu.be/abc123")
        again = await pipeline.process_media("https://youtu.be/abc123")
        self.assertEqual(again.status, "skipped")
