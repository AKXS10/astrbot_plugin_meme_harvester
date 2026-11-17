"""回归：CC 轨内容过少时改选更丰富的 AI 轨。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harvester.config import Settings
from harvester.extractor import MemeExtractor
from harvester.models import Track, VideoInfo
from harvester.pipeline import VideoPipeline
from harvester.storage import Storage


def make_info() -> VideoInfo:
    return VideoInfo(
        bvid="BV1xx411c7mD",
        aid=100,
        cid=200,
        title="测试视频",
        owner_name="UP",
        owner_mid=9,
        pubdate=1700000000,
        duration=600,
        page_no=1,
        pages=[{"cid": 200, "page": 1, "part": "P1", "duration": 600}],
    )


class TrackChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "harvester.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def test_short_cc_falls_back_to_richer_ai(self) -> None:
        class Client:
            async def get_video_info(self, *, bvid: str = "", aid: int = 0) -> VideoInfo:
                return make_info()

            async def get_tracks(self, *, bvid: str, cid: int, aid: int = 0):
                return (
                    [
                        Track(lan="zh-CN", url="https://sub/cc.json", ai_status=0),
                        Track(lan="ai-zh", url="https://sub/ai.json", ai_status=2, is_ai=True),
                    ],
                    "wbi",
                )

            async def resolve_ai_subtitle_url(self, *, aid: int, cid: int) -> str:
                return ""

            async def fetch_subtitle(self, url: str) -> dict:
                if "cc" in url:
                    return {"body": [{"from": 0, "to": 3, "content": "↓↓敲重点↓↓"}]}
                body = [{"from": 0, "to": 3, "content": "这是一段比较完整的字幕内容" * 20}]
                return {"body": body}

        async def complete(system: str, user: str) -> str:
            return "{}"

        pipeline = VideoPipeline(
            Client(),  # type: ignore[arg-type]
            self.storage,
            MemeExtractor(complete),
            lambda: Settings(review_required=False),
        )
        result = await pipeline.process_video("BV1xx411c7mD")
        self.assertEqual(result.status, "done")
        self.assertEqual(result.track_lan, "ai-zh")
        self.assertEqual(result.provider, "ai")


if __name__ == "__main__":
    unittest.main()
