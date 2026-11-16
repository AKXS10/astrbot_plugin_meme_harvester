"""HTTP 传输层：生产用 aiohttp，测试可注入假传输。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset=["']?\s*([\w-]+)""", re.IGNORECASE
)


@dataclass(slots=True)
class HttpResponse:
    status: int
    text: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    set_cookies: list[str] = field(default_factory=list)
    truncated: bool = False


def decode_body(raw: bytes, headers: dict[str, str] | None = None) -> str:
    """按响应头 / meta 猜测编码，失败时退回 utf-8 replace。"""
    headers = headers or {}
    charset = ""
    match = _CHARSET_RE.search(headers.get("content-type", "") or "")
    if match:
        charset = match.group(1)
    if not charset:
        match = _META_CHARSET_RE.search(raw[:4096] or b"")
        if match:
            charset = match.group(1).decode("ascii", "ignore")
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return raw.decode(candidate, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


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
        max_bytes: int | None = None,
    ) -> HttpResponse:
        import asyncio

        session = self._ensure_session()
        async with session.get(url, params=params, headers=headers, allow_redirects=True) as resp:
            raw_headers = {k.lower(): v for k, v in resp.headers.items()}
            set_cookies = list(resp.headers.getall("Set-Cookie", []))
            truncated = False
            if max_bytes:
                buffer = bytearray()
                try:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        buffer.extend(chunk)
                        if len(buffer) >= int(max_bytes):
                            truncated = True
                            break
                except asyncio.CancelledError:
                    raise
                text = decode_body(bytes(buffer), raw_headers)
            else:
                try:
                    text = await resp.text(errors="ignore")
                except asyncio.CancelledError:
                    raise
            return HttpResponse(
                status=resp.status,
                text=text,
                url=str(resp.url),
                headers=raw_headers,
                set_cookies=set_cookies,
                truncated=truncated,
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
