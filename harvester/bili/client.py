"""B 站 API 客户端：限流、WBI 签名、风控熔断、错误码映射。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..constants import (
    BILI_MAIN_HOST,
    BILI_PASSPORT_HOST,
    BILI_REFERER,
    DEFAULT_UA,
    ErrorCode,
    ERROR_MESSAGES,
)
from ..models import Track, VideoInfo
from .cookies import cookie_header, merge_set_cookies, parse_cookie_string
from .http import HttpResponse
from .wbi import sign_params

LOGIN_CODES = {-101, -352, 61000}
RISK_CODES = {-412, -799, -509}
NOT_FOUND_CODES = {-404, 62002, 62004, 62012}


class BiliError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code: ErrorCode, message: str = "", api_code: int | None = None) -> None:
        self.code = code
        self.api_code = api_code
        super().__init__(message or ERROR_MESSAGES.get(code, code.value))


class BiliClient:
    def __init__(
        self,
        transport,
        *,
        cookies: dict[str, str] | str | None = None,
        user_agent: str = DEFAULT_UA,
        interval: float = 1.0,
        risk_cooldown: float = 1800.0,
    ) -> None:
        self._transport = transport
        self.cookies: dict[str, str] = (
            parse_cookie_string(cookies) if isinstance(cookies, str) else dict(cookies or {})
        )
        self.user_agent = user_agent or DEFAULT_UA
        self.interval = max(0.0, float(interval))
        self.risk_cooldown = risk_cooldown
        self._last_request = 0.0
        self._throttle_lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._wbi_img = ""
        self._wbi_sub = ""
        self._wbi_at = 0.0
        self.stats = {"requests": 0, "risk_hits": 0}

    # ---------------------------------------------------------------- 基础

    async def close(self) -> None:
        await self._transport.close()

    def is_risk_blocked(self) -> bool:
        return time.monotonic() < self._blocked_until

    def _trip_breaker(self) -> None:
        self._blocked_until = time.monotonic() + self.risk_cooldown
        self.stats["risk_hits"] += 1

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            wait = self.interval - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _headers(self, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer or BILI_REFERER,
            "Origin": BILI_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        cookie = cookie_header(self.cookies)
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        referer: str | None = None,
        allow_html: bool = False,
        host: str = BILI_MAIN_HOST,
    ) -> dict[str, Any]:
        data, _ = await self._request(
            path,
            params,
            signed=signed,
            referer=referer,
            allow_html=allow_html,
            host=host,
        )
        return data

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        referer: str | None = None,
        allow_html: bool = False,
        host: str = BILI_MAIN_HOST,
    ) -> tuple[dict[str, Any], HttpResponse]:
        if self.is_risk_blocked():
            raise BiliError(ErrorCode.RISK_CONTROL)
        await self._throttle()
        query_params: dict[str, Any] = dict(params or {})
        if signed:
            img_key, sub_key = await self._ensure_wbi()
            query_params = sign_params(query_params, img_key, sub_key)
        url = host + path
        self.stats["requests"] += 1
        response: HttpResponse = await self._transport.get(
            url, params=query_params, headers=self._headers(referer)
        )
        merge_set_cookies(self.cookies, response.set_cookies)
        if response.status == 412:
            self._trip_breaker()
            raise BiliError(ErrorCode.RISK_CONTROL, api_code=-412)
        if response.status in (401, 403) and "application/json" not in response.headers.get("content-type", ""):
            raise BiliError(ErrorCode.SUBTITLE_EXPIRED, api_code=response.status)
        if response.status >= 500:
            raise BiliError(ErrorCode.INTERNAL, f"B 站返回 {response.status}")
        try:
            payload = json.loads(response.text)
        except ValueError:
            if allow_html:
                return {}, response
            raise BiliError(ErrorCode.INTERNAL, "B 站返回了非 JSON 内容") from None
        if not isinstance(payload, dict):
            raise BiliError(ErrorCode.INTERNAL, "B 站返回结构异常")
        code = int(payload.get("code") or 0)
        if code != 0:
            self._raise_api_error(code, str(payload.get("message") or ""))
        data = payload.get("data")
        return (data if isinstance(data, dict) else {}), response

    def _raise_api_error(self, code: int, message: str) -> None:
        if code in RISK_CODES:
            self._trip_breaker()
            raise BiliError(ErrorCode.RISK_CONTROL, message, api_code=code)
        if code in LOGIN_CODES:
            raise BiliError(ErrorCode.LOGIN_EXPIRED, message, api_code=code)
        if code in NOT_FOUND_CODES:
            raise BiliError(ErrorCode.VIDEO_NOT_FOUND, message, api_code=code)
        raise BiliError(ErrorCode.INTERNAL, message or f"B 站接口错误 {code}", api_code=code)

    # ---------------------------------------------------------------- WBI

    async def _ensure_wbi(self) -> tuple[str, str]:
        if self._wbi_img and self._wbi_sub and time.monotonic() - self._wbi_at < 600:
            return self._wbi_img, self._wbi_sub
        data = await self._get("/x/web-interface/nav", allow_html=True)
        wbi = data.get("wbi_img") or {}
        img = _key_from_url(str(wbi.get("img_url") or ""))
        sub = _key_from_url(str(wbi.get("sub_url") or ""))
        if not img or not sub:
            raise BiliError(ErrorCode.INTERNAL, "无法获取 WBI 密钥")
        self._wbi_img, self._wbi_sub, self._wbi_at = img, sub, time.monotonic()
        return img, sub

    # ---------------------------------------------------------------- 账号

    async def get_nav(self) -> dict[str, Any]:
        data = await self._get("/x/web-interface/nav", allow_html=True)
        wbi = data.get("wbi_img") or {}
        img = _key_from_url(str(wbi.get("img_url") or ""))
        sub = _key_from_url(str(wbi.get("sub_url") or ""))
        if img and sub:
            self._wbi_img, self._wbi_sub, self._wbi_at = img, sub, time.monotonic()
        return data

    def update_cookies(self, raw: str | dict[str, str]) -> None:
        incoming = parse_cookie_string(raw) if isinstance(raw, str) else dict(raw or {})
        self.cookies.update(incoming)

    def export_cookies(self) -> dict[str, str]:
        """返回当前合并后的 cookie 集，供加密持久化。"""
        return dict(self.cookies)

    async def qr_generate(self) -> dict[str, Any]:
        """申请扫码登录二维码（passport 主机）。"""
        data = await self._get(
            "/x/passport-login/web/qrcode/generate",
            host=BILI_PASSPORT_HOST,
        )
        return {
            "url": str(data.get("url") or ""),
            "qrcode_key": str(data.get("qrcode_key") or ""),
        }

    async def qr_poll(self, qrcode_key: str) -> dict[str, Any]:
        """轮询扫码状态；成功后 Set-Cookie 已被合并进 self.cookies。"""
        data = await self._get(
            "/x/passport-login/web/qrcode/poll",
            {"qrcode_key": qrcode_key},
            host=BILI_PASSPORT_HOST,
        )
        return {
            "code": int(data.get("code") or 0),
            "message": str(data.get("message") or ""),
            "url": str(data.get("url") or ""),
        }

    # ---------------------------------------------------------------- 视频

    async def get_video_info(self, *, bvid: str = "", aid: int = 0) -> VideoInfo:
        params: dict[str, Any] = {}
        if bvid:
            params["bvid"] = bvid
        elif aid:
            params["aid"] = aid
        else:
            raise BiliError(ErrorCode.BAD_URL, "缺少 bvid/aid")
        data = await self._get("/x/web-interface/view", params)
        pages = data.get("pages") or []
        first = pages[0] if pages else {}
        return VideoInfo(
            bvid=str(data.get("bvid") or bvid),
            aid=int(data.get("aid") or aid or 0),
            cid=int(first.get("cid") or data.get("cid") or 0),
            title=str(data.get("title") or ""),
            owner_name=str((data.get("owner") or {}).get("name") or ""),
            owner_mid=int((data.get("owner") or {}).get("mid") or 0),
            pubdate=int(data.get("pubdate") or 0),
            duration=int(data.get("duration") or 0),
            pages=[
                {
                    "cid": int(p.get("cid") or 0),
                    "page": int(p.get("page") or 1),
                    "part": str(p.get("part") or ""),
                    "duration": int(p.get("duration") or 0),
                }
                for p in pages
            ],
        )

    async def get_tracks(self, *, bvid: str, cid: int, aid: int = 0) -> tuple[list[Track], str]:
        """四级端点兜底：player_wbi → player_v2 → dm_view。返回 (tracks, provider)。"""
        base = {"bvid": bvid, "cid": cid}
        if aid:
            base["aid"] = aid
        attempts = (
            ("wbi", "/x/player/wbi/v2", True),
            ("player_v2", "/x/player/v2", False),
            ("dm", "/x/v2/dm/view", False),
        )
        last_error: BiliError | None = None
        succeeded = False
        for provider, path, signed in attempts:
            params = dict(base)
            if provider == "dm":
                params = {"oid": cid, "type": 1, "bvid": bvid}
            try:
                data = await self._get(path, params, signed=signed)
            except BiliError as exc:
                # 风控立即中断；未登录则继续尝试下一个端点（dm_view 有时不需要登录态）
                if exc.code == ErrorCode.RISK_CONTROL:
                    raise
                last_error = exc
                continue
            succeeded = True
            exclusive = bool(data.get("is_upower_exclusive"))
            playable = bool(data.get("is_upower_play"))
            if exclusive and not playable:
                raise BiliError(ErrorCode.CHARGE_BLOCKED)
            subtitle = data.get("subtitle") or {}
            raw_tracks = subtitle.get("subtitles") or []
            tracks = [Track.from_api(item) for item in raw_tracks if isinstance(item, dict)]
            if tracks:
                return tracks, provider
        if last_error is not None and last_error.code == ErrorCode.LOGIN_EXPIRED:
            raise last_error
        if not succeeded and last_error is not None:
            # 所有端点都失败：不要伪装成「没有字幕」，把真实原因带出去
            raise BiliError(
                ErrorCode.NO_SUBTITLE,
                f"字幕接口均失败（{last_error.code.value}：{last_error}）",
            )
        return [], ""

    async def resolve_ai_subtitle_url(self, *, aid: int, cid: int) -> str:
        if not aid or not cid:
            return ""
        data = await self._get("/x/player/v2/ai/subtitle/search/stat", {"aid": aid, "cid": cid})
        subtitle = data.get("subtitle") if isinstance(data.get("subtitle"), dict) else {}
        for candidate in (data.get("subtitle_url"), subtitle.get("subtitle_url"), data.get("url")):
            if candidate:
                return str(candidate)
        return ""

    async def fetch_subtitle(self, url: str) -> dict[str, Any]:
        if self.is_risk_blocked():
            raise BiliError(ErrorCode.RISK_CONTROL)
        await self._throttle()
        self.stats["requests"] += 1
        response: HttpResponse = await self._transport.get(url, headers=self._headers())
        merge_set_cookies(self.cookies, response.set_cookies)
        if response.status in (401, 403, 404):
            raise BiliError(ErrorCode.SUBTITLE_EXPIRED, api_code=response.status)
        try:
            return json.loads(response.text)
        except ValueError:
            raise BiliError(ErrorCode.SUBTITLE_EXPIRED, "字幕文件不是 JSON") from None

    # ---------------------------------------------------------------- 合集

    async def get_season_archives(
        self, *, mid: int, season_id: int, page_num: int = 1, page_size: int = 30
    ) -> tuple[list[VideoInfo], int]:
        referer = f"https://space.bilibili.com/{mid}/channel/collectiondetail?sid={season_id}"
        data = await self._get(
            "/x/polymer/web-space/seasons_archives_list",
            {"mid": mid, "season_id": season_id, "page_num": page_num, "page_size": page_size},
            referer=referer,
        )
        archives = data.get("archives") or []
        meta = data.get("meta") or {}
        total = int(meta.get("total") or len(archives))
        return [self._archive_to_video(item, season_id=season_id) for item in archives], total

    async def get_series_archives(
        self, *, mid: int, series_id: int, page_num: int = 1, page_size: int = 30
    ) -> tuple[list[VideoInfo], int]:
        data = await self._get(
            "/x/series/archives",
            {"mid": mid, "series_id": series_id, "pn": page_num, "ps": page_size, "order": "asc"},
            referer=f"https://space.bilibili.com/{mid}/channel/seriesdetail?sid={series_id}",
        )
        archives = data.get("archives") or []
        page = data.get("page") or {}
        total = int(page.get("total") or len(archives))
        return [self._archive_to_video(item, series_id=series_id) for item in archives], total

    @staticmethod
    def _archive_to_video(item: dict[str, Any], season_id: int = 0, series_id: int = 0) -> VideoInfo:
        return VideoInfo(
            bvid=str(item.get("bvid") or ""),
            aid=int(item.get("aid") or 0),
            cid=int(item.get("cid") or 0),
            title=str(item.get("title") or ""),
            pubdate=int(item.get("pubdate") or 0),
            duration=int(item.get("duration") or 0),
            season_id=season_id,
        )

    async def resolve_short_link(self, url: str) -> str:
        """短链跟随重定向，返回最终 URL。"""
        await self._throttle()
        response = await self._transport.get(url, headers=self._headers())
        return response.url or url


def _key_from_url(url: str) -> str:
    if not url:
        return ""
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1]
    return name.split(".")[0]


def extract_bvid_from_url(url: str) -> str:
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return ""
    return str(query.get("bvid", [""])[0])
