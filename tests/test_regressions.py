"""回归测试：新式合集链接、av 号、force 透传、失败原因落库、任务类型过滤。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harvester.bili.client import BiliError
from harvester.bili.urls import ParsedTarget, parse_target
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.extractor import MemeExtractor
from harvester.pipeline import VideoPipeline, _split_video_key
from harvester.storage import Storage
from harvester.tasks import TaskQueue


def make_settings() -> Settings:
    return Settings()


class ListsUrlTests(unittest.TestCase):
    def test_lists_season(self) -> None:
        target = parse_target("https://space.bilibili.com/1544008396/lists/1733148?type=season")
        self.assertEqual(target.kind, "season")
        self.assertEqual(target.mid, 1544008396)
        self.assertEqual(target.season_id, 1733148)

    def test_lists_series(self) -> None:
        target = parse_target("https://space.bilibili.com/1544008396/lists/1733148?type=series")
        self.assertEqual(target.kind, "series")
        self.assertEqual(target.series_id, 1733148)

    def test_lists_without_trailing_type_defaults_season(self) -> None:
        target = parse_target("https://space.bilibili.com/1544008396/lists/1733148")
        self.assertEqual(target.kind, "season")


class VideoKeyTests(unittest.TestCase):
    def test_split_av(self) -> None:
        self.assertEqual(_split_video_key("av170001"), ("", 170001))

    def test_split_bv(self) -> None:
        self.assertEqual(_split_video_key("BV1xx411c7mD"), ("BV1xx411c7mD", 0))


class AvPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp.cleanup()

    async def test_av_key_passes_aid(self) -> None:
        calls: dict[str, int] = {}

        class Client:
            async def get_video_info(self, *, bvid: str = "", aid: int = 0):
                calls["bvid"] = bvid
                calls["aid"] = aid
                raise BiliError(ErrorCode.VIDEO_NOT_FOUND, "视频不存在")

        async def complete(system: str, user: str) -> str:
            return "{}"

        pipeline = VideoPipeline(
            Client(),  # type: ignore[arg-type]
            self.storage,
            MemeExtractor(complete),
            make_settings,
        )
        with self.assertRaises(BiliError):
            await pipeline.process_video("av170001")
        self.assertEqual(calls["aid"], 170001)
        self.assertEqual(calls["bvid"], "")


class FailedPayloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp.cleanup()

    async def test_failure_writes_message_and_stage(self) -> None:
        class Pipeline:
            async def process_video(self, target, *, page=1, force=False, progress=None):
                if progress:
                    await progress("fetching_subtitle", 30)
                raise BiliError(ErrorCode.LOGIN_EXPIRED, "账号未登录")

        queue = TaskQueue(self.storage, Pipeline(), make_settings)
        task_id = await queue.submit("video", "BV1xx411c7mD", {})
        record = await queue.wait(task_id, timeout=5)
        self.assertIsNotNone(record)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["last_error"], "LOGIN_EXPIRED")
        self.assertEqual(record["payload"]["result"]["message"], "账号未登录")
        self.assertEqual(record["payload"]["stage"], "fetching_subtitle")


class CollectionForceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tmp.name) / "t.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp.cleanup()

    async def test_force_propagates(self) -> None:
        seen: dict[str, object] = {}

        class Pipeline:
            async def process_collection(self, target: ParsedTarget, *, force=False, progress=None):
                seen["force"] = force
                seen["kind"] = target.kind
                return {"total": 1, "done": 1, "failed": 0, "skipped": 0}

        queue = TaskQueue(self.storage, Pipeline(), make_settings)
        task_id = await queue.submit(
            "collection",
            "https://space.bilibili.com/1/lists/2?type=season",
            {"target_kind": "season", "mid": 1, "season_id": 2, "force": True},
        )
        record = await queue.wait(task_id, timeout=5)
        self.assertEqual(record["status"], "done")
        self.assertTrue(seen["force"])
        self.assertEqual(seen["kind"], "season")

    async def test_list_tasks_kind_filter(self) -> None:
        await self.storage.create_task("video", "BV1", {})
        await self.storage.create_task("collection", "https://space/1/lists/2", {})
        only_video = await self.storage.list_tasks(limit=10, kind="video")
        self.assertEqual([task["kind"] for task in only_video], ["video"])


if __name__ == "__main__":
    unittest.main()
