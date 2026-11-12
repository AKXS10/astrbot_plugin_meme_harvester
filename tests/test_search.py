"""检索与注入测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harvester.config import Settings
from harvester.models import MemeCandidate
from harvester.search import INJECT_HEADER, MemeSearch
from harvester.storage import Storage


class SearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "harvester.db")
        await self.storage.open()
        self.settings = Settings(inject_enabled=True, inject_max_chars=600)
        self.search = MemeSearch(self.storage, lambda: self.settings)

        await self.storage.upsert_meme(
            MemeCandidate(
                term="尊嘟假嘟",
                definition="真的假的的可爱化谐音",
                usage="表示怀疑",
                confidence=0.9,
                aliases=["尊嘟"],
            ),
            origin="BV1",
            default_status="published",
            min_confidence=0.0,
        )
        await self.storage.upsert_meme(
            MemeCandidate(term="哈基米", definition="形容可爱的小动物", confidence=0.9),
            origin="BV1",
            default_status="published",
            min_confidence=0.0,
        )
        await self.storage.upsert_slang(
            "group:123", "本地黑话", "本群内部才懂的词", aliases=["黑话"]
        )

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def test_lookup_hits_slang_first(self) -> None:
        hits = await self.search.lookup("这个本地黑话是什么意思", scope="group:123")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["source"], "slang")
        self.assertEqual(hits[0]["term"], "本地黑话")

    async def test_lookup_matches_meme_in_sentence(self) -> None:
        hits = await self.search.lookup("你这是在尊嘟假嘟")
        self.assertEqual([h["term"] for h in hits], ["尊嘟假嘟"])

    async def test_lookup_scope_isolation(self) -> None:
        hits = await self.search.lookup("本地黑话", scope="group:999")
        self.assertEqual(hits, [])

    async def test_build_injection(self) -> None:
        block = await self.search.build_injection("你这是在尊嘟假嘟", scope="group:123")
        self.assertIn(INJECT_HEADER, block)
        self.assertIn("尊嘟假嘟", block)
        self.assertLessEqual(len(block), 620)

    async def test_build_injection_empty_when_no_hit(self) -> None:
        block = await self.search.build_injection("今天天气不错")
        self.assertEqual(block, "")

    async def test_build_injection_disabled(self) -> None:
        self.settings = Settings(inject_enabled=False)
        block = await self.search.build_injection("你这是在尊嘟假嘟")
        self.assertEqual(block, "")

    async def test_describe(self) -> None:
        text = await self.search.describe("尊嘟假嘟")
        self.assertIn("真的假的", text)
        self.assertIn("用法", text)

    async def test_describe_unknown(self) -> None:
        self.assertEqual(await self.search.describe("不存在的词"), "")


if __name__ == "__main__":
    unittest.main()
