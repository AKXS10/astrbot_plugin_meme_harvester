"""链接解析测试。"""

from __future__ import annotations

import unittest

from harvester.bili.urls import parse_target


class UrlTests(unittest.TestCase):
    def test_bv_url_with_page(self) -> None:
        target = parse_target("https://www.bilibili.com/video/BV1xx411c7mD/?p=3&spm_id_from=333")
        self.assertEqual(target.kind, "video")
        self.assertEqual(target.bvid, "BV1xx411c7mD")
        self.assertEqual(target.page, 3)

    def test_bare_bv(self) -> None:
        self.assertEqual(parse_target("BV1xx411c7mD").bvid, "BV1xx411c7mD")

    def test_av_url(self) -> None:
        target = parse_target("https://www.bilibili.com/video/av170001")
        self.assertEqual(target.kind, "video")
        self.assertEqual(target.aid, 170001)

    def test_short_link(self) -> None:
        self.assertEqual(parse_target("https://b23.tv/abcdEF").kind, "short")

    def test_season_link(self) -> None:
        target = parse_target("https://space.bilibili.com/123456/channel/collectiondetail?sid=789")
        self.assertEqual(target.kind, "season")
        self.assertEqual(target.mid, 123456)
        self.assertEqual(target.season_id, 789)

    def test_series_link(self) -> None:
        target = parse_target("https://space.bilibili.com/123456/channel/seriesdetail?sid=456")
        self.assertEqual(target.kind, "series")
        self.assertEqual(target.series_id, 456)

    def test_space_video_with_sid(self) -> None:
        target = parse_target("https://space.bilibili.com/123456/video?sid=456")
        self.assertEqual(target.kind, "season")
        self.assertEqual(target.season_id, 456)

    def test_reject_foreign_domain(self) -> None:
        self.assertEqual(parse_target("https://evil.example.com/video/BV1xx411c7mD").kind, "unsupported")

    def test_reject_bad_text(self) -> None:
        self.assertEqual(parse_target("随便一段话").kind, "unsupported")
        self.assertEqual(parse_target("").kind, "unsupported")

    def test_host_prefix_not_allowed(self) -> None:
        self.assertEqual(parse_target("https://bilibili.com.evil.com/video/BV1xx411c7mD").kind, "unsupported")


if __name__ == "__main__":
    unittest.main()
