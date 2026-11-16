"""多来源解析测试：VTT 解析、链接分类、SSRF 防护、网页 / Wiki 抓取。"""

from __future__ import annotations

import json
import unittest

from harvester.bili.client import BiliError
from harvester.config import Settings
from harvester.constants import ErrorCode
from harvester.sources.classify import classify_url, normalize_url
from harvester.sources.vtt import parse_srt, parse_subtitle_text, parse_vtt
from harvester.sources.webpage import (
    SSRFGuard,
    WebPageResolver,
    extract_readable,
    looks_like_wiki,
    wiki_page_title,
)

from tests.helpers import FakeTransport

VTT = """WEBVTT
Kind: captions
Language: zh

00:00:00.000 --> 00:00:03.000
<c>今天讲一个梗</c>

00:00:03.000 --> 00:00:06.000
今天讲一个梗叫做尊嘟假嘟

00:00:06.000 --> 00:00:09.000
尊嘟假嘟就是真的假的的谐音
"""

SRT = """1
00:00:00,000 --> 00:00:02,500
第一句台词

2
00:00:02,500 --> 00:00:05,000
第二句台词
"""


def public_guard() -> SSRFGuard:
    return SSRFGuard(resolver=lambda host: ["93.184.216.34"])


class SubtitleParseTests(unittest.TestCase):
    def test_vtt_rolling_dedupe(self) -> None:
        segments = parse_vtt(VTT)
        texts = [s.text for s in segments]
        self.assertIn("尊嘟假嘟就是真的假的的谐音", texts)
        self.assertNotIn("今天讲一个梗", texts)  # 被更长的后续字幕覆盖
        self.assertEqual(segments[0].start_ms, 3000)

    def test_vtt_strips_tags_and_entities(self) -> None:
        segments = parse_vtt(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<b>你好</b> &amp; 世界\n"
        )
        self.assertEqual(segments[0].text, "你好 & 世界")
        self.assertEqual(segments[0].start_ms, 1000)

    def test_srt(self) -> None:
        segments = parse_srt(SRT)
        self.assertEqual([s.text for s in segments], ["第一句台词", "第二句台词"])
        self.assertEqual(segments[1].start_ms, 2500)

    def test_parse_subtitle_text_by_extension(self) -> None:
        self.assertEqual(len(parse_subtitle_text(SRT, "srt")), 2)
        self.assertEqual(len(parse_subtitle_text(VTT, "vtt")), 2)
        self.assertEqual(parse_subtitle_text("", "vtt"), [])


class ClassifyTests(unittest.TestCase):
    def test_bilibili(self) -> None:
        self.assertEqual(classify_url("BV1xx411c7mD").kind, "bilibili")
        self.assertEqual(classify_url("av170001").kind, "bilibili")
        self.assertEqual(
            classify_url("https://www.bilibili.com/video/BV1xx411c7mD?p=2").kind,
            "bilibili",
        )
        self.assertEqual(classify_url("https://b23.tv/abcdef").kind, "bilibili")

    def test_media(self) -> None:
        route = classify_url("https://www.youtube.com/watch?v=abc123")
        self.assertEqual(route.kind, "media")
        self.assertEqual(route.platform, "youtube")
        self.assertEqual(classify_url("https://v.douyin.com/iAbCdE/").platform, "douyin")
        self.assertEqual(classify_url("https://files.example.com/a/b.mp4").kind, "media")

    def test_wiki_and_web(self) -> None:
        self.assertEqual(classify_url("https://zh.moegirl.org.cn/尊嘟假嘟").kind, "wiki")
        self.assertEqual(classify_url("https://en.wikipedia.org/wiki/Meme").kind, "wiki")
        self.assertEqual(classify_url("https://example.com/post/1").kind, "web")

    def test_unsupported(self) -> None:
        self.assertEqual(classify_url("ftp://example.com/a").kind, "unsupported")
        self.assertEqual(classify_url("not a url").kind, "unsupported")

    def test_normalize_url(self) -> None:
        self.assertEqual(normalize_url("example.com/a"), "https://example.com/a")

    def test_wiki_helpers(self) -> None:
        self.assertTrue(looks_like_wiki("https://zh.moegirl.org.cn/梗"))
        self.assertEqual(wiki_page_title("https://zh.moegirl.org.cn/尊嘟假嘟"), "尊嘟假嘟")
        self.assertEqual(
            wiki_page_title("https://en.wikipedia.org/wiki/Rick_Astley"), "Rick Astley"
        )


class SSRFGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocks_private_targets(self) -> None:
        guard = SSRFGuard(resolver=lambda host: ["10.0.0.5"])
        for url in ("http://127.0.0.1/x", "http://localhost:8080/", "http://example.com/x"):
            with self.assertRaises(BiliError) as ctx:
                await guard.check(url)
            self.assertEqual(ctx.exception.code, ErrorCode.SSRF_BLOCKED)

    async def test_blocks_non_http(self) -> None:
        with self.assertRaises(BiliError):
            await SSRFGuard(resolver=lambda host: ["93.184.216.34"]).check("file:///etc/passwd")

    async def test_allows_public(self) -> None:
        await public_guard().check("https://zh.moegirl.org.cn/梗")


class ReadableTests(unittest.TestCase):
    def test_prefers_article(self) -> None:
        html = (
            "<html><head><title>条目 - 萌娘百科</title></head><body>"
            "<nav>导航 首页 分类</nav>"
            "<article><p>这是正文第一段，内容足够长。</p><p>这是正文第二段。</p></article>"
            "<footer>版权信息</footer></body></html>"
        )
        title, blocks = extract_readable(html)
        self.assertEqual(title, "条目")
        joined = "\n".join(blocks)
        self.assertIn("正文第一段", joined)
        self.assertNotIn("导航", joined)
        self.assertNotIn("版权信息", joined)

    def test_strips_scripts_and_refs(self) -> None:
        html = "<div class='mw-parser-output'><p>正文[1]内容</p><script>x()</script></div>"
        _, blocks = extract_readable(html)
        self.assertEqual(blocks, ["正文内容"])


class WebResolverTests(unittest.IsolatedAsyncioTestCase):
    def make_settings(self, **kwargs) -> Settings:
        values = {"web_min_chars": 5, "max_source_chars": 10_000, "web_timeout": 5}
        values.update(kwargs)
        return Settings(**values)

    async def test_mediawiki_api(self) -> None:
        def handler(url, params, headers):
            if "/api.php" in url:
                return {
                    "parse": {
                        "title": "尊嘟假嘟",
                        "text": "<div class='mw-parser-output'><p>萌娘百科的正文内容，讲的是这个梗。</p></div>",
                    }
                }
            return "<html></html>"

        resolver = WebPageResolver(
            FakeTransport(handler), lambda: self.make_settings(), guard=public_guard()
        )
        doc = await resolver.fetch("https://zh.moegirl.org.cn/尊嘟假嘟")
        self.assertEqual(doc.provider, "wiki")
        self.assertEqual(doc.title, "尊嘟假嘟")
        self.assertIn("萌娘百科的正文内容", doc.text)
        self.assertTrue(doc.source_id.startswith("web:"))
        self.assertTrue(doc.segments)

    async def test_html_fallback(self) -> None:
        html = "<html><head><title>示例文章</title></head><body><article><p>{}</p></article></body></html>".format(
            "这是一段足够长的网页正文。" * 10
        )

        def handler(url, params, headers):
            if "/api.php" in url:
                return {"error": {"code": "missingtitle"}}
            return html

        resolver = WebPageResolver(
            FakeTransport(handler), lambda: self.make_settings(), guard=public_guard()
        )
        doc = await resolver.fetch("https://zh.moegirl.org.cn/示例")
        self.assertEqual(doc.provider, "wiki")
        self.assertIn("足够长的网页正文", doc.text)

    async def test_web_content_empty(self) -> None:
        resolver = WebPageResolver(
            FakeTransport(lambda *a: "<html><body><p>短</p></body></html>"),
            lambda: self.make_settings(web_min_chars=500),
            guard=public_guard(),
        )
        with self.assertRaises(BiliError) as ctx:
            await resolver.fetch("https://example.com/a")
        self.assertEqual(ctx.exception.code, ErrorCode.WEB_CONTENT_EMPTY)

    async def test_http_error(self) -> None:
        from harvester.bili.http import HttpResponse

        resolver = WebPageResolver(
            FakeTransport(lambda *a: HttpResponse(403, "denied")),
            lambda: self.make_settings(),
            guard=public_guard(),
        )
        with self.assertRaises(BiliError) as ctx:
            await resolver.fetch("https://example.com/a")
        self.assertEqual(ctx.exception.code, ErrorCode.WEB_FETCH_FAILED)

    async def test_cookie_header_sent(self) -> None:
        seen: dict[str, str] = {}

        def handler(url, params, headers):
            seen.update(headers)
            body = "<article><p>{}</p></article>".format("这是一段网页正文。" * 20)
            return f"<html><head><title>示例</title></head><body>{body}</body></html>"

        resolver = WebPageResolver(
            FakeTransport(handler),
            lambda: self.make_settings(web_cookies="a=1; b=2"),
            guard=public_guard(),
        )
        await resolver.fetch("https://example.com/post")
        self.assertEqual(seen.get("Cookie"), "a=1; b=2")
