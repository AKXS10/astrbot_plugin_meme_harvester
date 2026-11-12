"""抽取器测试：JSON 容错、幻觉过滤、去重合并、快筛兜底。"""

from __future__ import annotations

import json
import unittest

from harvester.extractor import MemeExtractor
from harvester.llm import parse_llm_json
from harvester.models import Segment


def seg(i: int, text: str) -> Segment:
    return Segment(index=i, start_ms=i * 1000, end_ms=(i + 1) * 1000, text=text)


class ParseJsonTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        payload = parse_llm_json('{"memes": [{"term": "x"}]}')
        self.assertEqual(payload["memes"][0]["term"], "x")

    def test_code_fence(self) -> None:
        payload = parse_llm_json('```json\n{"memes": []}\n```')
        self.assertEqual(payload, {"memes": []})

    def test_prose_wrapped(self) -> None:
        payload = parse_llm_json('好的，结果如下：\n{"memes": [{"term": "a"}]}\n希望有帮助')
        self.assertEqual(payload["memes"][0]["term"], "a")

    def test_bare_list(self) -> None:
        payload = parse_llm_json('[{"term": "a"}]')
        self.assertEqual(payload["memes"][0]["term"], "a")

    def test_invalid(self) -> None:
        self.assertIsNone(parse_llm_json("完全不是 JSON"))
        self.assertIsNone(parse_llm_json(""))
        self.assertIsNone(parse_llm_json("{broken"))


class ExtractorTests(unittest.IsolatedAsyncioTestCase):
    def build(self, responses: list[str]) -> tuple[MemeExtractor, list[str]]:
        prompts: list[str] = []
        queue = list(responses)

        async def complete(system: str, user: str) -> str:
            prompts.append(user)
            return queue.pop(0) if queue else "{}"

        return MemeExtractor(complete), prompts

    async def test_extracts_and_validates(self) -> None:
        chunk = [
            seg(0, "今天讲一个梗叫做尊嘟假嘟"),
            seg(1, "尊嘟假嘟就是真的假的的谐音，弹幕都在刷"),
        ]
        response = json.dumps(
            {
                "memes": [
                    {
                        "term": "尊嘟假嘟",
                        "definition": "真的假的的可爱化谐音说法",
                        "usage": "表示怀疑时用",
                        "quote": "尊嘟假嘟就是真的假的的谐音",
                        "start_ms": 1000,
                        "confidence": 0.92,
                        "aliases": ["尊嘟"],
                        "tags": ["谐音梗"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        extractor, prompts = self.build([response])
        outcome = await extractor.extract("标题", "UP", [chunk])
        self.assertEqual(len(outcome.candidates), 1)
        candidate = outcome.candidates[0]
        self.assertEqual(candidate.term, "尊嘟假嘟")
        self.assertEqual(candidate.start_ms, 1000)
        self.assertEqual(len(prompts), 1)

    async def test_drops_hallucinated_quote(self) -> None:
        chunk = [seg(0, "今天天气不错")]
        response = json.dumps(
            {
                "memes": [
                    {"term": "编造梗", "definition": "不存在的解释", "quote": "字幕里根本没有这句", "confidence": 0.9}
                ]
            },
            ensure_ascii=False,
        )
        extractor, _ = self.build([response])
        outcome = await extractor.extract("标题", "UP", [chunk])
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.dropped, 1)

    async def test_falls_back_to_matching_segment(self) -> None:
        chunk = [seg(0, "他说这波操作泰裤辣"), seg(1, "泰裤辣懂吗")]
        response = json.dumps(
            {"memes": [{"term": "泰裤辣", "definition": "太酷啦谐音", "quote": "", "confidence": 0.8}]},
            ensure_ascii=False,
        )
        extractor, _ = self.build([response])
        outcome = await extractor.extract("标题", "UP", [chunk])
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0].quote, "他说这波操作泰裤辣")
        self.assertEqual(outcome.candidates[0].start_ms, 0)

    async def test_dedupes_across_chunks(self) -> None:
        chunk1 = [seg(0, "这个梗叫哈基米")]
        chunk2 = [seg(2, "哈基米这个梗哈基米")]
        response = json.dumps(
            {
                "memes": [
                    {"term": "哈基米", "definition": "短解释", "quote": "这个梗叫哈基米", "confidence": 0.6},
                ]
            },
            ensure_ascii=False,
        )
        response2 = json.dumps(
            {
                "memes": [
                    {
                        "term": "哈基米",
                        "definition": "更长更完整的解释内容",
                        "quote": "哈基米哈基米",
                        "confidence": 0.9,
                        "aliases": ["hakimi"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        extractor, _ = self.build([response, response2])
        outcome = await extractor.extract("标题", "UP", [chunk1, chunk2])
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(outcome.candidates[0].definition, "更长更完整的解释内容")
        self.assertIn("hakimi", outcome.candidates[0].aliases)

    async def test_invalid_json_counts_as_dropped(self) -> None:
        extractor, _ = self.build(["这不是 JSON"])
        outcome = await extractor.extract("标题", "UP", [[seg(0, "有个梗")]])
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.dropped, 1)

    async def test_chunks_without_keywords_use_fallback(self) -> None:
        chunks = [[seg(0, "普通内容")], [seg(1, "更多普通内容")], [seg(2, "第三段")]]
        extractor, prompts = self.build(["{}"])
        outcome = await extractor.extract("标题", "UP", chunks)
        self.assertTrue(outcome.used_fallback)
        self.assertEqual(outcome.chunks_sent, 2)
        self.assertEqual(len(prompts), 2)

    async def test_low_quality_items_dropped(self) -> None:
        chunk = [seg(0, "啊这")]
        response = json.dumps(
            {
                "memes": [
                    {"term": "啊", "definition": "太短", "quote": "啊这"},
                    {"term": "", "definition": "空词名", "quote": "啊这"},
                    {"term": "无释义梗", "definition": "", "quote": "啊这"},
                ]
            },
            ensure_ascii=False,
        )
        extractor, _ = self.build([response])
        outcome = await extractor.extract("标题", "UP", [chunk])
        self.assertEqual(outcome.candidates, [])
        self.assertEqual(outcome.dropped, 3)


if __name__ == "__main__":
    unittest.main()
