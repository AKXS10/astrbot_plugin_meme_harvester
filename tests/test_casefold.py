"""大小写归一化回归：词条名、别名、群黑话都应按大小写不敏感匹配。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harvester.models import MemeCandidate
from harvester.storage import Storage
from harvester.text import normalize_term


class CaseFoldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "harvester.db")
        await self.storage.open()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self._tmp.cleanup()

    def test_normalize_mixed_case(self) -> None:
        self.assertEqual(normalize_term("K神"), "k神")
        self.assertEqual(normalize_term("KSKBL"), "kskbl")
        self.assertEqual(normalize_term("  kskbl! "), "kskbl")

    async def test_meme_dedupe_ignores_case(self) -> None:
        first = await self.storage.upsert_meme(
            MemeCandidate(term="K神", definition="混排词名"), default_status="published"
        )
        second = await self.storage.upsert_meme(
            MemeCandidate(term="k神", definition="混排词名"), default_status="published"
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.meme_id, second.meme_id)
        self.assertEqual(await self.storage.count_memes(), 1)

    async def test_alias_merge_ignores_case(self) -> None:
        meme = await self.storage.upsert_meme(
            MemeCandidate(term="康神开播了", definition="缩写", aliases=["KSKBL"]),
            default_status="published",
        )
        row = await self.storage.find_meme("康神开播了")
        self.assertIsNotNone(row)
        await self.storage.upsert_meme(
            MemeCandidate(term="康神开播了", definition="缩写", aliases=["kskbl", "ZDJD"]),
            default_status="published",
        )
        row = await self.storage.find_meme("康神开播了")
        aliases = row["aliases"]
        if isinstance(aliases, str):
            aliases = json.loads(aliases or "[]")
        self.assertEqual([a.lower() for a in aliases].count("kskbl"), 1)
        self.assertIn("ZDJD", aliases)
        self.assertEqual(meme.meme_id, row["id"])

    async def test_lookup_matches_alias_ignoring_case(self) -> None:
        await self.storage.upsert_meme(
            MemeCandidate(
                term="康神开播了",
                definition="KSKBL = 康神开播了，ZDJD = 真的假的。",
                aliases=["kskbl", "zdjd"],
            ),
            default_status="published",
        )
        hits = await self.storage.match_memes_in_text("有人刷 KSKBL 了吗")
        self.assertEqual([h["term"] for h in hits], ["康神开播了"])
        hits = await self.storage.match_memes_in_text("回个 ZdJd 试试")
        self.assertEqual([h["term"] for h in hits], ["康神开播了"])

    async def test_list_memes_query_ignores_case(self) -> None:
        await self.storage.upsert_meme(
            MemeCandidate(term="康神开播了", definition="缩写", aliases=["kskbl"]),
            default_status="published",
        )
        for query in ("KSKBL", "kskbl", "Kskbl"):
            hits = await self.storage.list_memes(query)
            self.assertEqual([h["term"] for h in hits], ["康神开播了"], query)

    async def test_slang_matches_alias_and_case(self) -> None:
        await self.storage.upsert_slang(
            scope="group:1",
            term="康神开播了",
            definition="接 ZDJD",
            aliases=["KSKBL"],
        )
        rows = await self.storage.find_slangs("group:1", "你们刷 kskbl 干嘛")
        self.assertEqual([r["term"] for r in rows], ["康神开播了"])
        rows = await self.storage.find_slangs("group:1", "KSKBL")
        self.assertEqual(len(rows), 1)
        self.assertEqual(await self.storage.find_slangs("group:2", "kskbl"), [])
