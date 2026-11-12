"""WBI 与字幕解析测试。"""

from __future__ import annotations

import unittest

from harvester.bili.subtitle import (
    parse_subtitle_payload,
    pick_track,
    provider_label,
    rank_tracks,
    segments_text_hash,
    track_kind,
)
from harvester.bili.wbi import mixin_key, sign_params
from harvester.models import Track


class WbiTests(unittest.TestCase):
    def test_documented_mixin_vector(self) -> None:
        # 来自 bilibili-API-collect 的公开示例
        self.assertEqual(
            mixin_key(
                "7cd084941338484aae1ad9425b84077c",
                "4932caff0ff746eab6f01bf08b70ac45",
            ),
            "ea1db124af3c7062474693fa704f4ff8",
        )

    def test_sign_params_adds_valid_rid(self) -> None:
        signed = sign_params(
            {"foo": "114", "bar": "514"},
            "7cd084941338484aae1ad9425b84077c",
            "4932caff0ff746eab6f01bf08b70ac45",
            wts=1700000000,
        )
        self.assertEqual(signed["wts"], "1700000000")
        self.assertEqual(len(signed["w_rid"]), 32)
        int(signed["w_rid"], 16)  # 必须是合法十六进制

    def test_sign_params_filters_forbidden_chars(self) -> None:
        signed = sign_params({"q": "a!b'c(d)e*f"}, "x" * 32, "y" * 32, wts=1)
        self.assertEqual(signed["q"], "abcdef")


class SubtitleTests(unittest.TestCase):
    def test_rank_prefers_chinese_cc(self) -> None:
        tracks = [
            Track(lan="en-US", url="u-en", ai_status=0),
            Track(lan="ai-zh", url="u-ai", ai_status=1, is_ai=True),
            Track(lan="zh-CN", url="u-cc", ai_status=0),
            Track(lan="ai-en", url="", ai_status=1, is_ai=True),
        ]
        ranked = rank_tracks(tracks)
        self.assertEqual(ranked[0].lan, "zh-CN")
        self.assertEqual(ranked[1].lan, "ai-zh")
        self.assertEqual(track_kind(pick_track(tracks)), "cc")

    def test_track_without_url_ranks_lower(self) -> None:
        tracks = [Track(lan="zh-CN", url="", ai_status=0), Track(lan="zh-Hans", url="u", ai_status=0)]
        self.assertEqual(pick_track(tracks).url, "u")

    def test_parse_payload(self) -> None:
        payload = {
            "body": [
                {"from": 1.5, "to": 3.25, "content": "第一句"},
                {"from": 3.25, "to": 4.0, "content": "  第二句\n换行  "},
                {"from": 5.0, "to": 4.0, "content": ""},
            ]
        }
        segments = parse_subtitle_payload(payload)
        self.assertEqual([s.text for s in segments], ["第一句", "第二句 换行"])
        self.assertEqual(segments[0].start_ms, 1500)
        self.assertEqual(segments[0].end_ms, 3250)
        self.assertEqual([s.index for s in segments], [0, 1])

    def test_parse_payload_tolerates_garbage(self) -> None:
        self.assertEqual(parse_subtitle_payload(None), [])
        self.assertEqual(parse_subtitle_payload({"body": "x"}), [])
        self.assertEqual(parse_subtitle_payload({"body": [{"content": "ok", "from": "x"}]})[0].start_ms, 0)

    def test_text_hash_is_stable_and_sensitive(self) -> None:
        a = parse_subtitle_payload({"body": [{"from": 0, "to": 1, "content": "你好"}]})
        b = parse_subtitle_payload({"body": [{"from": 0, "to": 9, "content": "你好"}]})
        c = parse_subtitle_payload({"body": [{"from": 0, "to": 1, "content": "再见"}]})
        self.assertEqual(segments_text_hash(a), segments_text_hash(b))
        self.assertNotEqual(segments_text_hash(a), segments_text_hash(c))

    def test_provider_labels(self) -> None:
        cc = Track(lan="zh-CN", url="u")
        ai = Track(lan="ai-zh", url="u", is_ai=True)
        self.assertEqual(provider_label(cc, "wbi"), "cc")
        self.assertEqual(provider_label(ai, "wbi"), "ai")
        self.assertEqual(provider_label(ai, "wbi", resolved_ai=True), "ai_stat")
        self.assertEqual(provider_label(cc, "dm"), "dm")


if __name__ == "__main__":
    unittest.main()
