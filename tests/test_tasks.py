"""任务队列测试：成功、失败、同步任务、取消。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from harvester.bili.client import BiliError
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.models import SyncResult
from harvester.pipeline import VideoResult
from harvester.storage import Storage
from harvester.tasks import DynamicLimiter, TaskQueue


class FakePipeline:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.delay = 0.0
        self.calls: list[tuple] = []

    async def process_video(self, bvid, *, page=1, force=False, progress=None):
        self.calls.append(("video", bvid, page, force))
        if progress is not None:
            await progress("fetching_subtitle", 20)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return VideoResult(bvid=bvid, status="done", memes_created=2)

    async def process_collection(self, target, *, force=False, progress=None):
        self.calls.append(("collection", target.kind, target.season_id))
        if progress is not None:
            await progress("collection 1/2", 50)
        return {"total": 2, "processed": 2, "done": 2}


class TaskQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "harvester.db")
        await self.storage.open()
        self.settings = Settings(queue_concurrency=1)
        self.pipeline = FakePipeline()
        self.queue = TaskQueue(self.storage, self.pipeline, lambda: self.settings)
        await self.queue.start()

    async def asyncTearDown(self) -> None:
        await self.queue.shutdown()
        await self.storage.close()
        self._tmp.cleanup()

    async def test_video_task_done(self) -> None:
        task_id = await self.queue.submit("video", "BV1xx411c7mD", {"page": 2})
        snapshot = await self.queue.wait(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["progress"], 100)
        self.assertEqual(snapshot["payload"]["result"]["memes_created"], 2)
        self.assertEqual(self.pipeline.calls[0], ("video", "BV1xx411c7mD", 2, False))

    async def test_failure_marks_failed(self) -> None:
        self.pipeline.error = BiliError(ErrorCode.NO_SUBTITLE)
        task_id = await self.queue.submit("video", "BV1xx411c7mD")
        snapshot = await self.queue.wait(task_id)
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["last_error"], "NO_SUBTITLE")

    async def test_collection_task(self) -> None:
        task_id = await self.queue.submit(
            "collection", "x", {"target_kind": "season", "season_id": 42}
        )
        snapshot = await self.queue.wait(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["payload"]["result"]["total"], 2)
        self.assertEqual(self.pipeline.calls[0], ("collection", "season", 42))

    async def test_sync_task(self) -> None:
        async def sync_fn():
            return SyncResult(scanned=1, synced=1, mode="inprocess")

        queue = TaskQueue(
            self.storage, self.pipeline, lambda: self.settings, sync_fn=sync_fn
        )
        await queue.start()
        task_id = await queue.submit("sync", "memewiki")
        snapshot = await queue.wait(task_id)
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["payload"]["result"]["synced"], 1)
        await queue.shutdown()

    async def test_cancel_running_task(self) -> None:
        self.pipeline.delay = 1.0
        task_id = await self.queue.submit("video", "BV1xx411c7mD")
        for _ in range(40):
            current = await self.storage.get_task(task_id)
            if current and current["status"] == "running":
                break
            await asyncio.sleep(0.05)
        self.assertTrue(self.queue.cancel(task_id))
        snapshot = await self.queue.wait(task_id, timeout=2)
        self.assertEqual(snapshot["status"], "cancelled")

    async def test_progress_is_persisted(self) -> None:
        task_id = await self.queue.submit("video", "BV1xx411c7mD")
        snapshot = await self.queue.wait(task_id)
        self.assertEqual(snapshot["payload"]["stage"], "fetching_subtitle")


class DynamicLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_shrinking_limit_never_over_issues(self) -> None:
        """上限从 3 调到 1 时，已在跑的不会被脱管，新任务也不会瞬间超发。"""
        limiter = DynamicLimiter()
        active = 0
        peak = 0
        release = asyncio.Event()

        async def worker(limit: int) -> None:
            nonlocal active, peak
            async with limiter.slot(limit):
                active += 1
                peak = max(peak, active)
                await release.wait()
                active -= 1

        tasks = [asyncio.create_task(worker(3)) for _ in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(peak, 3)
        # 运行中把上限压到 1：再来两个任务必须排队，active 不能变成 5
        tasks += [asyncio.create_task(worker(1)) for _ in range(2)]
        await asyncio.sleep(0.05)
        self.assertEqual(active, 3)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main()
