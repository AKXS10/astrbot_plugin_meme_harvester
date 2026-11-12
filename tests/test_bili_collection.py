"""合集分页收集测试。"""

from __future__ import annotations

import unittest

from harvester.bili.collection import collect_target
from harvester.bili.urls import ParsedTarget
from harvester.models import VideoInfo


class FakeClient:
    def __init__(self, pages: list[list[VideoInfo]], total: int) -> None:
        self.pages = pages
        self.total = total
        self.season_calls: list[dict] = []
        self.series_calls: list[dict] = []

    async def get_season_archives(self, *, mid, season_id, page_num=1, page_size=30):
        self.season_calls.append({"mid": mid, "season_id": season_id, "page": page_num, "size": page_size})
        index = page_num - 1
        return (self.pages[index] if index < len(self.pages) else []), self.total

    async def get_series_archives(self, *, mid, series_id, page_num=1, page_size=30):
        self.series_calls.append({"mid": mid, "series_id": series_id, "page": page_num, "size": page_size})
        index = page_num - 1
        return (self.pages[index] if index < len(self.pages) else []), self.total

    @staticmethod
    def make(bvid: str) -> VideoInfo:
        return VideoInfo(bvid=bvid, aid=1, title=bvid)


def page(*bvids: str) -> list[VideoInfo]:
    return [FakeClient.make(b) for b in bvids]


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_pages(self) -> None:
        client = FakeClient([page("BV1", "BV2"), page("BV3")], total=3)
        target = ParsedTarget(kind="season", raw="x", mid=1, season_id=2)
        videos, total = await collect_target(client, target, page_size=2, max_items=100)
        self.assertEqual(total, 3)
        self.assertEqual([v.bvid for v in videos], ["BV1", "BV2", "BV3"])
        self.assertEqual(len(client.season_calls), 2)

    async def test_stops_at_max_items(self) -> None:
        client = FakeClient([page("BV1", "BV2", "BV3"), page("BV4")], total=4)
        target = ParsedTarget(kind="season", raw="x", mid=1, season_id=2)
        videos, _ = await collect_target(client, target, page_size=3, max_items=2)
        self.assertEqual([v.bvid for v in videos], ["BV1", "BV2"])
        self.assertEqual(len(client.season_calls), 1)

    async def test_dedupes_repeated_bvid(self) -> None:
        client = FakeClient([page("BV1", "BV1"), page("BV1", "BV2")], total=2)
        target = ParsedTarget(kind="series", raw="x", mid=1, series_id=3)
        videos, _ = await collect_target(client, target, page_size=2, max_items=10)
        self.assertEqual([v.bvid for v in videos], ["BV1", "BV2"])
        self.assertEqual(len(client.series_calls), 2)

    async def test_reports_progress(self) -> None:
        client = FakeClient([page("BV1"), page("BV2")], total=2)
        target = ParsedTarget(kind="season", raw="x", mid=1, season_id=2)
        seen: list[tuple[int, int, int]] = []

        async def on_page(page_no: int, collected: int, total: int) -> None:
            seen.append((page_no, collected, total))

        await collect_target(client, target, page_size=1, max_items=10, on_page=on_page)
        self.assertEqual(seen, [(1, 1, 2), (2, 2, 2)])

    async def test_non_collection_returns_empty(self) -> None:
        client = FakeClient([], total=0)
        videos, total = await collect_target(client, ParsedTarget(kind="video", raw="x"))
        self.assertEqual(videos, [])
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
