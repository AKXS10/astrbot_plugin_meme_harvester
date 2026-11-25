"""B 站客户端测试：错误映射、风控熔断、端点兜底、签名。"""

from __future__ import annotations

import unittest

from harvester.bili.client import BiliClient, BiliError
from harvester.bili.http import HttpResponse
from harvester.constants import BILI_PASSPORT_HOST, ErrorCode
from tests.helpers import FakeTransport, api_err, api_ok

NAV = api_ok(
    {
        "isLogin": True,
        "mid": 42,
        "uname": "测试号",
        "wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        },
    }
)

VIEW = api_ok(
    {
        "bvid": "BV1xx411c7mD",
        "aid": 100,
        "cid": 200,
        "title": "标题",
        "owner": {"name": "UP", "mid": 9},
        "pubdate": 1700000000,
        "duration": 300,
        "pages": [{"cid": 200, "page": 1, "part": "P1", "duration": 300}],
    }
)


def make_client(handler, **kwargs) -> tuple[BiliClient, FakeTransport]:
    transport = FakeTransport(handler)
    client = BiliClient(transport, interval=0, **kwargs)
    return client, transport


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_info_parsed(self) -> None:
        client, _ = make_client(lambda url, params, headers: VIEW)
        info = await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertEqual(info.aid, 100)
        self.assertEqual(info.cid, 200)
        self.assertEqual(info.title, "标题")
        self.assertEqual(info.owner_mid, 9)
        self.assertEqual(info.video_key, "BV1xx411c7mD")

    async def test_risk_control_trips_breaker(self) -> None:
        def handler(url, params, headers):
            return HttpResponse(412, "<html>blocked</html>")

        client, transport = make_client(handler)
        with self.assertRaises(BiliError) as ctx:
            await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.RISK_CONTROL)
        with self.assertRaises(BiliError) as second:
            await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertEqual(second.exception.code, ErrorCode.RISK_CONTROL)
        self.assertEqual(len(transport.calls), 1)  # 第二次没有发请求

    async def test_qr_poll_missing_code_is_not_success(self) -> None:
        """缺 code 不能当成 QR_SUCCESS(0)，否则会被误判成「登录态无效，请重新扫码」。"""
        client, _ = make_client(lambda url, params, headers: api_ok({"message": "风控页"}))
        data = await client.qr_poll("KEY")
        self.assertEqual(data["code"], -1)

    async def test_breaker_is_per_host(self) -> None:
        """主站被 -412 熔断后，护照站（扫码登录）不应被一起锁死。"""

        def handler(url, params, headers):
            return HttpResponse(412, "<html>blocked</html>")

        client, _ = make_client(handler)
        with self.assertRaises(BiliError):
            await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertTrue(client.is_risk_blocked())
        self.assertFalse(client.is_risk_blocked(BILI_PASSPORT_HOST))
        self.assertEqual(client.stats["risk_hits"], 1)

    async def test_login_expired_mapping(self) -> None:
        client, _ = make_client(lambda url, params, headers: api_err(-101, "账号未登录"))
        with self.assertRaises(BiliError) as ctx:
            await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.LOGIN_EXPIRED)

    async def test_not_found_mapping(self) -> None:
        client, _ = make_client(lambda url, params, headers: api_err(-404, "啥都木有"))
        with self.assertRaises(BiliError) as ctx:
            await client.get_video_info(bvid="BV1xx411c7mD")
        self.assertEqual(ctx.exception.code, ErrorCode.VIDEO_NOT_FOUND)

    async def test_tracks_use_signed_endpoint_only(self) -> None:
        """字幕轨只从签名端点读取；未签名端点会返回别的视频的字幕。"""

        def handler(url, params, headers):
            if "/x/web-interface/nav" in url:
                return NAV
            if "/x/player/wbi/v2" in url:
                return api_ok({"subtitle": {"subtitles": [{"lan": "zh-CN", "subtitle_url": "https://s/1.json"}]}})
            if "/x/player/v2" in url:
                return api_ok({"subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "https://s/wrong.json"}]}})
            return api_err(-404)

        client, transport = make_client(handler)
        tracks, provider = await client.get_tracks(bvid="BV1xx411c7mD", cid=200, aid=100)
        self.assertEqual(provider, "wbi")
        self.assertEqual([t.lan for t in tracks], ["zh-CN"])
        self.assertFalse(any("/x/player/v2" in c["url"] for c in transport.calls))
        self.assertFalse(any("/x/v2/dm/view" in c["url"] for c in transport.calls))
        wbi_call = next(c for c in transport.calls if "/x/player/wbi/v2" in c["url"])
        self.assertIn("w_rid", wbi_call["params"])
        self.assertIn("wts", wbi_call["params"])

    async def test_tracks_login_expired_propagates(self) -> None:
        def handler(url, params, headers):
            if "/x/web-interface/nav" in url:
                return NAV
            return api_err(-101, "账号未登录")

        client, _ = make_client(handler)
        with self.assertRaises(BiliError) as ctx:
            await client.get_tracks(bvid="BV1xx411c7mD", cid=200)
        self.assertEqual(ctx.exception.code, ErrorCode.LOGIN_EXPIRED)

    async def test_charge_blocked(self) -> None:
        def handler(url, params, headers):
            if "/x/web-interface/nav" in url:
                return NAV
            return api_ok({"is_upower_exclusive": True, "is_upower_play": False, "subtitle": {"subtitles": []}})

        client, _ = make_client(handler, cookies={"SESSDATA": "x"})
        with self.assertRaises(BiliError) as ctx:
            await client.get_tracks(bvid="BV1xx411c7mD", cid=200, aid=100)
        self.assertEqual(ctx.exception.code, ErrorCode.CHARGE_BLOCKED)

    async def test_subtitle_expired_on_403(self) -> None:
        client, _ = make_client(lambda url, params, headers: HttpResponse(403, "forbidden"))
        with self.assertRaises(BiliError) as ctx:
            await client.fetch_subtitle("https://aisubtitle.hdslb.com/x.json?auth_key=1")
        self.assertEqual(ctx.exception.code, ErrorCode.SUBTITLE_EXPIRED)

    async def test_nav_merges_set_cookie(self) -> None:
        response = HttpResponse(
            200,
            __import__("json").dumps(NAV, ensure_ascii=False),
            url="https://api.bilibili.com/x/web-interface/nav",
            set_cookies=["buvid3=abc123; Path=/; Domain=bilibili.com"],
        )
        client, _ = make_client(lambda url, params, headers: response, cookies={"SESSDATA": "s"})
        nav = await client.get_nav()
        self.assertTrue(nav["isLogin"])
        self.assertEqual(client.cookies["buvid3"], "abc123")
        self.assertEqual(client.cookies["SESSDATA"], "s")
        self.assertTrue(client._wbi_img.startswith("7cd084"))

    async def test_season_archives(self) -> None:
        payload = api_ok(
            {
                "archives": [
                    {"aid": 1, "bvid": "BV1aa411c7mD", "title": "合集第一集", "pubdate": 1, "duration": 60},
                    {"aid": 2, "bvid": "BV1bb411c7mD", "title": "合集第二集", "pubdate": 2, "duration": 70},
                ],
                "meta": {"total": 120},
            }
        )
        client, transport = make_client(lambda url, params, headers: payload)
        videos, total = await client.get_season_archives(mid=42, season_id=7, page_num=2, page_size=30)
        self.assertEqual(total, 120)
        self.assertEqual([v.bvid for v in videos], ["BV1aa411c7mD", "BV1bb411c7mD"])
        self.assertEqual(videos[0].season_id, 7)
        call = transport.calls[0]
        self.assertIn("space.bilibili.com/42", call["headers"]["Referer"])
        self.assertEqual(call["params"]["page_num"], 2)


if __name__ == "__main__":
    unittest.main()
