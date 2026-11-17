"""来源分类、任务清除/去重、字幕临时文件保留。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from harvester.models import MemeCandidate, VideoInfo
from harvester.storage import Storage
from harvester.tasks import TaskQueue
from harvester.tempstore import TempStore


def make_web_info() -> VideoInfo:
    return VideoInfo(
        bvid="",
        aid=0,
        cid=0,
        title="某视频标题",
        owner_name="某UP主",
        owner_mid=1,
        pubdate=0,
        duration=100,
        platform="web",
        video_id_override="web:abcdef1234567890",
        url="https://example.com/post/1",
    )


class SourceLabelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "h.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def test_bilibili_uses_bvid(self) -> None:
        info = VideoInfo(
            bvid="BV1xx411c7mD",
            aid=1,
            cid=2,
            title="标题",
            owner_name="UP",
            owner_mid=3,
            pubdate=0,
            duration=10,
            url="https://www.bilibili.com/video/BV1xx411c7mD",
        )
        await self.storage.upsert_video(info, "processing")
        row = await self.storage.get_video("BV1xx411c7mD")
        self.assertEqual(row["source_label"], "BV1xx411c7mD")

    async def test_other_platform_uses_author_and_title(self) -> None:
        info = make_web_info()
        await self.storage.upsert_video(info, "processing")
        row = await self.storage.get_video("web:abcdef1234567890", platform="web")
        self.assertEqual(row["source_label"], "某UP主｜某视频标题")

    async def test_list_memes_carries_source_label(self) -> None:
        info = make_web_info()
        vid = await self.storage.upsert_video(info, "processing")
        candidate = MemeCandidate(
            term="测试梗",
            definition="测试释义",
            quote="某视频标题",
            confidence=0.9,
        )
        await self.storage.upsert_meme(
            candidate, info.url, vid, default_status="published", min_confidence=0.6
        )
        rows = await self.storage.list_memes(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_label"], "某UP主｜某视频标题")

    async def test_list_sources_groups_by_video(self) -> None:
        info = make_web_info()
        await self.storage.upsert_video(info, "done")
        rows = await self.storage.list_sources(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_label"], "某UP主｜某视频标题")
        self.assertEqual(rows[0]["url"], info.url)


class TempStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TempStore(Path(self._tmp.name) / "temp")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_save_and_stats(self) -> None:
        path = self.store.save("BV1xx", title="标题", provider="cc", text="第一句\n第二句")
        self.assertTrue(Path(path).exists())
        data = Path(path).read_text(encoding="utf-8")
        self.assertIn("BV1xx", data)
        self.assertIn("第一句", data)
        stats = self.store.stats()
        self.assertEqual(stats["files"], 1)
        self.assertGreater(stats["bytes"], 0)

    def test_cleanup_removes_only_expired(self) -> None:
        old = Path(self.store.save("old", title="旧", provider="cc", text="旧内容"))
        new = Path(self.store.save("new", title="新", provider="cc", text="新内容"))
        stale = time.time() - 5 * 86400
        os.utime(old, (stale, stale))
        removed = self.store.cleanup(days=3)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_name_is_sanitized(self) -> None:
        path = Path(self.store.save("web:../../etc/passwd", title="x", provider="web", text="x"))
        self.assertEqual(path.parent, self.store.dir)
        self.assertNotIn("..", path.name)


class FakePipeline:
    def __init__(self) -> None:
        self.started: asyncio.Event | None = None

    async def process_video(self, target: str, *, page: int = 1, force: bool = False, progress=None):
        if self.started is not None:
            self.started.set()
        await asyncio.sleep(10)
        return {"done": 1}


class TaskDedupeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "h.db")
        await self.storage.open()
        self.pipeline = FakePipeline()
        self.pipeline.started = asyncio.Event()
        self.queue = TaskQueue(self.storage, self.pipeline, lambda: None)

    async def asyncTearDown(self) -> None:
        await self.queue.shutdown()
        await asyncio.sleep(0.05)
        await self.storage.close()
        self._tmp.cleanup()

    async def test_resubmit_replaces_previous_task(self) -> None:
        first = await self.queue.submit("video", "BV1xx")
        await asyncio.wait_for(self.pipeline.started.wait(), timeout=2)
        second = await self.queue.submit("video", "BV1xx")
        rows = await self.storage.find_tasks_by_target("BV1xx")
        self.assertEqual([row["id"] for row in rows], [second])
        self.assertIsNone(await self.storage.get_task(first))

    async def test_clear_deletes_record(self) -> None:
        task_id = await self.queue.submit("video", "BV1yy")
        cleared = await self.queue.clear(task_id)
        self.assertTrue(cleared)
        self.assertIsNone(await self.storage.get_task(task_id))
        self.assertFalse(await self.queue.clear("missing"))

    def test_settings_asr_deps_path(self):
        from harvester.config import Settings

        settings = Settings.from_config({"asr_deps_path": "  ./deps  "})
        self.assertEqual(settings.asr_deps_path, "./deps")
        self.assertEqual(Settings.from_config({}).asr_deps_path, "")


if __name__ == "__main__":
    unittest.main()
