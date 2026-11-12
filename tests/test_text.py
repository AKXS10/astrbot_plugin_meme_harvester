"""文本工具测试：归一化、引文校验、分块。"""

from __future__ import annotations

import unittest

from harvester.models import Segment
from harvester.text import (
    clean_text,
    is_meaningful_term,
    normalize_term,
    quote_in_source,
    split_chunks,
    truncate,
)


class TextTests(unittest.TestCase):
    def test_clean_text_strips_zero_width(self) -> None:
        self.assertEqual(clean_text(" 你\u200b好\n世界 "), "你好 世界")

    def test_normalize_term_removes_punctuation(self) -> None:
        self.assertEqual(normalize_term("「尊嘟假嘟」"), "尊嘟假嘟")
        self.assertEqual(normalize_term("  yyds "), "yyds")

    def test_normalize_term_removes_inner_space_for_cjk(self) -> None:
        self.assertEqual(normalize_term("哈 基 米"), "哈基米")

    def test_is_meaningful_term(self) -> None:
        self.assertTrue(is_meaningful_term("尊嘟假嘟"))
        self.assertTrue(is_meaningful_term("yyds"))
        self.assertFalse(is_meaningful_term("啊"))
        self.assertFalse(is_meaningful_term(""))

    def test_quote_in_source_ignores_spaces(self) -> None:
        source = "然后他说 尊嘟假嘟 全场都笑了"
        self.assertTrue(quote_in_source("尊嘟假嘟", source))
        self.assertTrue(quote_in_source("他说 尊嘟假嘟", source))
        self.assertFalse(quote_in_source("这是编造的", source))

    def test_truncate(self) -> None:
        self.assertEqual(truncate("abcdef", 4), "abc…")
        self.assertEqual(truncate("abc", 4), "abc")
        self.assertEqual(truncate("abc", 0), "")

    def test_split_chunks_respects_limit(self) -> None:
        segments = [Segment(i, i * 1000, (i + 1) * 1000, "字" * 100) for i in range(20)]
        chunks = split_chunks(segments, chunk_chars=500, max_chunks=10, overlap_ms=120_000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(sum(len(s.text) for s in chunk), 500 + 100)
        total_unique = {id(seg) for chunk in chunks for seg in chunk}
        self.assertEqual(len(total_unique), 20)

    def test_split_chunks_handles_empty(self) -> None:
        self.assertEqual(split_chunks([]), [])
        self.assertEqual(split_chunks([Segment(0, 0, 1, "")]), [])

    def test_split_chunks_caps_count(self) -> None:
        segments = [Segment(i, i * 1000, (i + 1) * 1000, "字" * 100) for i in range(100)]
        chunks = split_chunks(segments, chunk_chars=500, max_chunks=3)
        self.assertEqual(len(chunks), 3)


if __name__ == "__main__":
    unittest.main()
