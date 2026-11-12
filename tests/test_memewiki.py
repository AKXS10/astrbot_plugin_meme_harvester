"""梗百科适配器测试：能力探测、只增不改、超长跳过、导出兜底。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from harvester.memewiki import MODE_EXPORT, MODE_INPROCESS, MODE_UNAVAILABLE, MemewikiAdapter
from harvester.models import MemeCandidate
from harvester.storage import Storage


class FakePlugin:
    def __init__(self) -> None:
        self._user_terms: dict[str, dict] = {
            "老词": {
                "meaning": "用户自己写的释义",
                "author": "webui",
                "author_name": "WebUI",
                "ts": "2024-01-01 00:00:00",
            }
        }
        self._terms_lock = asyncio.Lock()
        self.saved = 0
        self.invalidated: list[str] = []

    def _save_user_terms(self) -> None:
        self.saved += 1

    def _invalidate_term_cache(self, name: str = "") -> None:
        self.invalidated.append(name)


class FakeMeta:
    def __init__(self, plugin) -> None:
        self.star_cls = plugin


class FakeContext:
    def __init__(self, plugin=None) -> None:
        self.plugin = plugin

    def get_registered_star(self, name: str):
        if self.plugin is None or name != "astrbot_plugin_memewiki":
            return None
        return FakeMeta(self.plugin)


class MemewikiAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.storage = Storage(self.data_dir / "harvester.db")
        await self.storage.open()
        self.plugin = FakePlugin()
        self.adapter = MemewikiAdapter(FakeContext(self.plugin), self.storage, self.data_dir)

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    async def _seed(self, term: str, definition: str = "一条释义", status: str = "published") -> int:
        result = await self.storage.upsert_meme(
            MemeCandidate(term=term, definition=definition, confidence=0.95),
            origin="BV test",
            default_status=status,
            min_confidence=0.0,
        )
        return result.meme_id

    async def test_probe_inprocess(self) -> None:
        probe = self.adapter.probe()
        self.assertTrue(probe.available)
        self.assertEqual(probe.mode, MODE_INPROCESS)
        self.assertEqual(probe.terms, 1)

    async def test_probe_without_plugin(self) -> None:
        adapter = MemewikiAdapter(FakeContext(None), self.storage, self.data_dir)
        probe = adapter.probe()
        self.assertFalse(probe.available)
        self.assertEqual(probe.mode, MODE_UNAVAILABLE)

    async def test_probe_schema_mismatch_falls_back_to_export(self) -> None:
        broken = type("Broken", (), {"_user_terms": {}})
        adapter = MemewikiAdapter(FakeContext(broken), self.storage, self.data_dir)
        probe = adapter.probe()
        self.assertFalse(probe.available)
        self.assertEqual(probe.mode, MODE_EXPORT)

    async def test_sync_only_adds(self) -> None:
        new_id = await self._seed("新梗", definition="新的释义")
        old_id = await self._seed("老词", definition="我们的释义")
        result = await self.adapter.sync()
        self.assertEqual(result.synced, 1)
        self.assertEqual(result.existed, 1)
        self.assertEqual(self.plugin._user_terms["新梗"]["meaning"], "新的释义")
        # 只增不改：老词仍是用户原来的释义
        self.assertEqual(self.plugin._user_terms["老词"]["meaning"], "用户自己写的释义")
        self.assertEqual(self.plugin.saved, 1)
        self.assertIn("新梗", self.plugin.invalidated)

        new_row = await self.storage.get_meme(new_id)
        old_row = await self.storage.get_meme(old_id)
        self.assertEqual(new_row["sync_state"], "synced")
        self.assertEqual(old_row["sync_state"], "existed")

    async def test_sync_skips_long_term(self) -> None:
        long_term = "这是一个超过十六个字长度的梗名称啊"
        meme_id = await self._seed(long_term)
        result = await self.adapter.sync()
        self.assertEqual(result.skipped, 1)
        row = await self.storage.get_meme(meme_id)
        self.assertEqual(row["sync_state"], "skipped")
        self.assertIn("term_too_long", row["sync_error"])
        self.assertNotIn(long_term, self.plugin._user_terms)

    async def test_dry_run_does_not_mutate(self) -> None:
        await self._seed("新梗")
        result = await self.adapter.sync(dry_run=True)
        self.assertEqual(result.synced, 1)
        self.assertNotIn("新梗", self.plugin._user_terms)
        self.assertEqual(self.plugin.saved, 0)

    async def test_sync_unavailable_keeps_pending(self) -> None:
        adapter = MemewikiAdapter(FakeContext(None), self.storage, self.data_dir)
        meme_id = await self._seed("新梗")
        result = await adapter.sync()
        self.assertEqual(result.mode, MODE_UNAVAILABLE)
        self.assertEqual(result.skipped, 1)
        row = await self.storage.get_meme(meme_id)
        self.assertEqual(row["sync_state"], "pending")

    async def test_export_jsonl(self) -> None:
        await self._seed("新梗", definition="导出释义")
        path = await self.adapter.export_jsonl()
        self.assertTrue(path.exists())
        line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["name"], "新梗")
        self.assertEqual(line["meaning"], "导出释义")
        self.assertEqual(line["author_name"], "梗采集器")

    async def test_status_reports_pending(self) -> None:
        await self._seed("新梗")
        status = await self.adapter.status()
        self.assertEqual(status["mode"], MODE_INPROCESS)
        self.assertEqual(status["pending"], 1)


if __name__ == "__main__":
    unittest.main()
