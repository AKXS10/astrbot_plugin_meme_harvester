"""流水线测试：正常解析、跳过、失败状态、冲突审核、合集中断。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harvester.bili.client import BiliError
from harvester.bili.urls import ParsedTarget
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.extractor import MemeExtractor
from harvester.models import Track, VideoInfo
from harvester.pipeline import VideoPipeline
from harvester.storage import Storage

SUBTITLE_BODY = {
    "body": [
        {"from": 0.0, "to": 3.0, "content": "今天讲一个梗叫做尊嘟假嘟"},
        {"from": 3.0, "to": 6.0, "content": "尊嘟假嘟就是真的假的的谐音，弹幕都在刷"},
    ]
}

LLM_JSON = json.dumps(
    {
        "memes": [
            {
                "term": "尊嘟假嘟",
                "definition": "真的假的的可爱化谐音",
                "usage": "表示怀疑",
                "quote": "尊嘟假嘟就是真的假的的谐音",
                "start_ms": 3000,
                "confidence": 0.9,
                "aliases": ["尊嘟"],
                "tags": ["谐音梗"],
            }
        ]
    },
    ensure_ascii=False,
)


def make_info(bvid: str = "BV1xx411c7mD", pages: int = 1) -> VideoInfo:
    return VideoInfo(
        bvid=bvid,
        aid=100,
        cid=200,
        title="测试视频",
        owner_name="UP",
        owner_mid=9,
        pubdate=1700000000,
        duration=600,
        page_no=1,
        pages=[{"cid": 200 + i, "page": i + 1, "part": f"P{i + 1}", "duration": 600} for i in range(pages)],
    )


class FakeBiliClient:
    def __init__(self, info: VideoInfo, tracks: list[Track] | None = None, payload: dict | None = None) -> None:
        self.info = info
        self.tracks = tracks if tracks is not None else [Track(lan="zh-CN", url="https://sub/1.json", ai_status=0)]
        self.payload = payload if payload is not None else SUBTITLE_BODY
        self.fail_with: BiliError | None = None
        self.ai_url = ""
        self.calls: list[str] = []

    async def get_video_info(self, *, bvid: str = "", aid: int = 0) -> VideoInfo:
        self.calls.append("view")
        if self.fail_with is not None:
            raise self.fail_with
        return self.info

    async def get_tracks(self, *, bvid: str, cid: int, aid: int = 0):
        self.calls.append("tracks")
        if self.fail_with is not None:
            raise self.fail_with
        return self.tracks, "wbi"

    async def resolve_ai_subtitle_url(self, *, aid: int, cid: int) -> str:
        self.calls.append("ai_stat")
        return self.ai_url

    async def fetch_subtitle(self, url: str) -> dict:
        self.calls.append("subtitle")
        if self.fail_with is not None:
            raise self.fail_with
        return self.payload

    async def get_season_archives(self, *, mid, season_id, page_num=1, page_size=30):
        return [self.info], 1


def make_extractor(response: str = LLM_JSON) -> MemeExtractor:
    async def complete(system: str, user: str) -> str:
        return response

    return MemeExtractor(complete)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "harvester.db")
        await self.storage.open()
        self.settings = Settings(review_required=False, chunk_chars=6000, max_chunks=8)
        self.client = FakeBiliClient(make_info())
        self.pipeline = VideoPipeline(
            self.client, self.storage, make_extractor(), lambda: self.settings
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def test_happy_path(self) -> None:
        stages: list[tuple[str, int]] = []

        async def progress(stage: str, pct: int) -> None:
            stages.append((stage, pct))

        result = await self.pipeline.process_video("BV1xx411c7mD", progress=progress)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.provider, "cc")
        self.assertEqual(result.memes_created, 1)
        self.assertEqual(result.reviews, 0)
        self.assertIn(("done", 100), stages)
        self.assertGreaterEqual(await self.storage.count_memes("published"), 1)
        video = await self.storage.get_video("BV1xx411c7mD")
        self.assertEqual(video["status"], "done")
        transcript = await self.storage.get_transcript(video["id"])
        self.assertEqual(transcript["provider"], "cc")
        self.assertEqual(result.transcript_id, transcript["id"])
        meme = await self.storage.find_meme("尊嘟假嘟")
        self.assertEqual(meme["origin"], "https://www.bilibili.com/video/BV1xx411c7mD")

    async def test_second_run_skips(self) -> None:
        await self.pipeline.process_video("BV1xx411c7mD")
        again = await self.pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(again.status, "skipped")
        self.assertEqual(await self.storage.count_memes(), 1)

    async def test_subtitle_timeline_mismatch_rejected(self) -> None:
        """字幕时间轴远超视频时长时拒绝入库（防止取到别的视频的字幕）。"""
        self.client.payload = {
            "body": [
                {"from": 0.0, "to": 2.0, "content": "这是别的视频的第一句"},
                {"from": 1400.0, "to": 1500.0, "content": "这是别的视频的结尾"},
            ]
        }
        with self.assertRaises(BiliError) as ctx:
            await self.pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.SUBTITLE_MISMATCH)
        video = await self.storage.get_video("BV1xx411c7mD")
        self.assertEqual(video["status"], "failed")
        self.assertIn("SUBTITLE_MISMATCH", video["last_error"])
        self.assertEqual(await self.storage.count_memes(), 0)

    async def test_no_subtitle_marks_failed(self) -> None:
        self.client.tracks = []
        with self.assertRaises(BiliError) as ctx:
            await self.pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.NO_SUBTITLE)
        video = await self.storage.get_video("BV1xx411c7mD")
        self.assertEqual(video["status"], "failed")
        self.assertIn("NO_SUBTITLE", video["last_error"])

    async def test_charge_blocked_propagates(self) -> None:
        async def blocked(*args, **kwargs):
            raise BiliError(ErrorCode.CHARGE_BLOCKED)

        self.client.get_tracks = blocked  # type: ignore[method-assign]
        with self.assertRaises(BiliError) as ctx:
            await self.pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.CHARGE_BLOCKED)
        video = await self.storage.get_video("BV1xx411c7mD")
        self.assertEqual(video["status"], "failed")

    async def test_review_required_keeps_draft(self) -> None:
        pipeline = VideoPipeline(
            self.client,
            self.storage,
            make_extractor(),
            lambda: Settings(review_required=True),
        )
        result = await pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(result.status, "done")
        self.assertEqual(await self.storage.count_memes("draft"), 1)
        self.assertEqual(await self.storage.pending_sync_memes(), [])

    async def test_conflict_goes_to_review(self) -> None:
        await self.pipeline.process_video("BV1xx411c7mD", force=True)
        conflicting = json.dumps(
            {
                "memes": [
                    {
                        "term": "尊嘟假嘟",
                        "definition": "完全不同的解释",
                        "quote": "今天讲一个梗叫做尊嘟假嘟",
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )
        pipeline = VideoPipeline(
            self.client, self.storage, make_extractor(conflicting), lambda: self.settings
        )
        result = await pipeline.process_video("BV1xx411c7mD", force=True)
        self.assertEqual(result.conflicts, 1)
        self.assertEqual(result.reviews, 1)
        self.assertEqual(await self.storage.count_reviews(), 1)

    async def test_multi_page_selects_cid(self) -> None:
        self.client.info = make_info(pages=3)
        await self.pipeline.process_video("BV1xx411c7mD", page=3)
        video = await self.storage.get_video("BV1xx411c7mD_p3")
        self.assertIsNotNone(video)
        self.assertEqual(video["cid"], 202)

    async def test_ai_track_resolves_url(self) -> None:
        self.client.tracks = [Track(lan="ai-zh", url="", ai_status=1, is_ai=True)]
        self.client.ai_url = "https://aisubtitle.hdslb.com/x.json"
        result = await self.pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(result.provider, "ai_stat")
        self.assertIn("ai_stat", self.client.calls)

    async def test_collection_aborts_on_risk_control(self) -> None:
        self.client.fail_with = BiliError(ErrorCode.RISK_CONTROL)
        target = ParsedTarget(kind="season", raw="x", mid=9, season_id=1)
        summary = await self.pipeline.process_collection(target)
        self.assertEqual(summary["aborted"], "RISK_CONTROL")
        self.assertEqual(summary["total"], 1)

    async def test_collection_empty(self) -> None:
        async def empty(*args, **kwargs):
            return [], 0

        self.client.get_season_archives = empty  # type: ignore[method-assign]
        target = ParsedTarget(kind="season", raw="x", mid=9, season_id=1)
        with self.assertRaises(BiliError) as ctx:
            await self.pipeline.process_collection(target)
        self.assertEqual(ctx.exception.code, ErrorCode.COLLECTION_EMPTY)


if __name__ == "__main__":
    unittest.main()
