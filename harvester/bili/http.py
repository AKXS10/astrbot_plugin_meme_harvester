"""HTTP 传输层：生产用 aiohttp，测试可注入假传输。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HttpResponse:
    status: int
    text: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: list[str] = field(default_factory=list)


class AiohttpTransport:
    """基于 aiohttp 的真实传输。Cookie 由上层显式管理，不依赖会话罐。"""

    def __init__(self, timeout: float = 20.0) -> None:
        self._timeout = float(timeout)
        self._session = None

    def _ensure_session(self):
        """延迟到首次请求时创建：插件构造函数在同步上下文执行。"""
        if self._session is not None and not self._session.closed:
            return self._session
        import aiohttp  # 延迟导入，纯逻辑测试不需要

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            trust_env=False,
        )
        return self._session

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse:
        import asyncio

        session = self._ensure_session()
        async with session.get(url, params=params, headers=headers, allow_redirects=True) as resp:
            try:
                body = await resp.text(errors="ignore")
            except asyncio.CancelledError:
                raise
            raw_headers = {k.lower(): v for k, v in resp.headers.items()}
            set_cookies = list(resp.headers.getall("Set-Cookie", []))
            return HttpResponse(
                status=resp.status,
                text=body,
                url=str(resp.url),
                headers=raw_headers,
                set_cookies=set_cookies,
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
