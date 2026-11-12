"""存储层单元测试：验证建表、幂等、去重、冲突与检索。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harvester.models import MemeCandidate, Segment, VideoInfo
from harvester.storage import Storage


def make_video(bvid: str = "BV1xx411c7mD", page: int = 1, title: str = "测试视频") -> VideoInfo:
    return VideoInfo(
        bvid=bvid,
        aid=12345,
        cid=67890 + page,
        title=title,
        owner_name="UP主",
        owner_mid=999,
        pubdate=1700000000,
        duration=600,
        season_id=42,
        page_no=page,
    )


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_dir = Path(self._tmp.name)
        self.storage = Storage(self.db_dir / "harvester.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def test_open_creates_schema(self) -> None:
        stats = await self.storage.stats()
        self.assertEqual(stats["videos"], 0)
        self.assertEqual(stats["memes"], 0)
        self.assertTrue((self.db_dir / "harvester.db").exists())

    async def test_upsert_video_is_idempotent(self) -> None:
        first = await self.storage.upsert_video(make_video())
        second = await self.storage.upsert_video(make_video(title="改名了"))
        self.assertEqual(first, second)
        stats = await self.storage.stats()
        self.assertEqual(stats["videos"], 1)
        video = await self.storage.get_video("BV1xx411c7mD")
        self.assertIsNotNone(video)
        self.assertEqual(video["title"], "改名了")

    async def test_multi_page_video_has_distinct_key(self) -> None:
        await self.storage.upsert_video(make_video(page=1))
        await self.storage.upsert_video(make_video(page=2))
        stats = await self.storage.stats()
        self.assertEqual(stats["videos"], 2)
        self.assertIsNotNone(await self.storage.get_video("BV1xx411c7mD_p2"))

    async def test_transcript_dedupe(self) -> None:
        video_id = await self.storage.upsert_video(make_video())
        segments = [Segment(0, 0, 1000, "这是第一个梗"), Segment(1, 1000, 2000, "第二个")]
        inserted = await self.storage.add_transcript(video_id, "zh-CN", "cc", segments, "hash-1")
        again = await self.storage.add_transcript(video_id, "zh-CN", "cc", segments, "hash-1")
        self.assertTrue(inserted)
        self.assertFalse(again)

    async def test_purge_raw_transcripts(self) -> None:
        video_id = await self.storage.upsert_video(make_video())
        await self.storage.add_transcript(video_id, "ai-zh", "ai", [Segment(0, 0, 1, "x")], "h")
        purged = await self.storage.purge_raw_transcripts(days=0)
        self.assertEqual(purged, 0)
        # 手动把 fetched_at 挪到过去再清理
        def backdate() -> None:
            with self.storage._conn() as conn:
                conn.execute("UPDATE transcripts SET fetched_at='2000-01-01T00:00:00+00:00'")

        import asyncio

        await asyncio.to_thread(backdate)
        purged = await self.storage.purge_raw_transcripts(days=30)
        self.assertEqual(purged, 1)
        self.assertIsNone(await self.storage.get_transcript(video_id))

    async def test_upsert_meme_created_once(self) -> None:
        result = await self.storage.upsert_meme(
            MemeCandidate(term="尊嘟假嘟", definition="真的假的", confidence=0.9),
            origin="BV1xx411c7mD",
            default_status="published",
        )
        self.assertTrue(result.created)
        again = await self.storage.upsert_meme(
            MemeCandidate(term=" 尊嘟假嘟 ", usage="例句", aliases=["尊嘟"], confidence=0.95),
            origin="BV1xx411c7mD",
            default_status="published",
        )
        self.assertFalse(again.created)
        self.assertEqual(again.meme_id, result.meme_id)
        self.assertIn("usage", again.filled)
        meme = await self.storage.get_meme(result.meme_id)
        self.assertEqual(meme["usage"], "例句")
        self.assertEqual(meme["heat"], 2)

    async def test_upsert_meme_detects_conflict(self) -> None:
        first = await self.storage.upsert_meme(
            MemeCandidate(term="泰裤辣", definition="太酷啦的谐音"), default_status="published"
        )
        second = await self.storage.upsert_meme(
            MemeCandidate(term="泰裤辣", definition="完全不同的解释"), default_status="published"
        )
        self.assertEqual(first.meme_id, second.meme_id)
        self.assertTrue(second.conflict)

    async def test_invalid_term_rejected(self) -> None:
        result = await self.storage.upsert_meme(MemeCandidate(term="  "), default_status="published")
        self.assertEqual(result.meme_id, 0)
        self.assertEqual(await self.storage.count_memes(), 0)

    async def test_search_by_term_and_alias(self) -> None:
        await self.storage.upsert_meme(
            MemeCandidate(term="哈基米", aliases=["hakimi"], definition="可爱小动物的称呼"),
            default_status="published",
        )
        by_term = await self.storage.list_memes(query="哈基米")
        self.assertEqual(len(by_term), 1)
        by_alias = await self.storage.list_memes(query="hakimi")
        self.assertEqual(len(by_alias), 1)
        by_definition = await self.storage.list_memes(query="小动物")
        self.assertEqual(len(by_definition), 1)
        missing = await self.storage.list_memes(query="不存在的词")
        self.assertEqual(missing, [])

    async def test_pending_sync_only_published(self) -> None:
        draft = await self.storage.upsert_meme(
            MemeCandidate(term="草稿梗", definition="草稿"), default_status="draft"
        )
        published = await self.storage.upsert_meme(
            MemeCandidate(term="正式梗", definition="正式"), default_status="published"
        )
        pending = await self.storage.pending_sync_memes(limit=10)
        ids = {row["id"] for row in pending}
        self.assertIn(published.meme_id, ids)
        self.assertNotIn(draft.meme_id, ids)

    async def test_mark_synced(self) -> None:
        result = await self.storage.upsert_meme(
            MemeCandidate(term="已同步梗", definition="解释"), default_status="published"
        )
        await self.storage.mark_synced([result.meme_id], "synced", slug="bili:BV1:1000")
        meme = await self.storage.get_meme(result.meme_id)
        self.assertEqual(meme["sync_state"], "synced")
        self.assertEqual(meme["memewiki_slug"], "bili:BV1:1000")

    async def test_review_dedupe(self) -> None:
        first = await self.storage.add_review("conflict", {"term": "x"}, "冲突", dedupe_key="k1")
        second = await self.storage.add_review("conflict", {"term": "x"}, "冲突", dedupe_key="k1")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(await self.storage.count_reviews(), 1)
        self.assertTrue(await self.storage.resolve_review(first, "accepted"))
        self.assertEqual(await self.storage.count_reviews(), 0)

    async def test_task_lifecycle(self) -> None:
        task_id = await self.storage.create_task("video", "BV1xx411c7mD", {"bvid": "BV1xx411c7mD"})
        task = await self.storage.get_task(task_id)
        self.assertEqual(task["status"], "queued")
        await self.storage.update_task(task_id, status="running", progress=50)
        task = await self.storage.get_task(task_id)
        self.assertEqual(task["progress"], 50)
        self.assertEqual(await self.storage.fail_stale_tasks(), 1)
        task = await self.storage.get_task(task_id)
        self.assertEqual(task["status"], "failed")

    async def test_slang_scope_isolation(self) -> None:
        await self.storage.upsert_slang("group:1", "吃饭", "吃饭的意思", created_by="u1")
        await self.storage.upsert_slang("group:2", "睡觉", "睡觉的意思", created_by="u1")
        group1 = await self.storage.list_slangs("group:1")
        self.assertEqual(len(group1), 1)
        hits = await self.storage.find_slangs("group:1", "今天吃饭了吗")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["term"], "吃饭")
        self.assertEqual(await self.storage.find_slangs("group:1", "今天睡觉"), [])
        self.assertTrue(await self.storage.delete_slang("group:1", "吃饭"))
        self.assertEqual(await self.storage.list_slangs("group:1"), [])

    async def test_settings_roundtrip(self) -> None:
        await self.storage.set_setting("provider_id", "deepseek")
        await self.storage.set_setting("queue_concurrency", 2)
        settings = await self.storage.get_settings()
        self.assertEqual(settings["provider_id"], "deepseek")
        self.assertEqual(settings["queue_concurrency"], 2)
        self.assertEqual(settings["schema_version"], 1)

    async def test_accounts_latest(self) -> None:
        await self.storage.save_account(1, "甲", "enc1", source="cookie")
        await self.storage.save_account(2, "乙", "enc2", source="qrcode")
        latest = await self.storage.latest_account()
        self.assertIn(latest["mid"], (1, 2))
        accounts = await self.storage.list_accounts()
        self.assertEqual(len(accounts), 2)
        await self.storage.update_account_status(1, "expired")
        await self.storage.delete_account(2)
        self.assertEqual(len(await self.storage.list_accounts()), 1)


if __name__ == "__main__":
    unittest.main()
