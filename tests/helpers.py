"""测试辅助：可编程的假 HTTP 传输。"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from harvester.bili.http import HttpResponse


def api_ok(data: Any) -> dict[str, Any]:
    return {"code": 0, "message": "0", "data": data}


def api_err(code: int, message: str = "error") -> dict[str, Any]:
    return {"code": code, "message": message, "data": None}


class FakeTransport:
    """按路径分发的假传输，记录每次调用。"""

    def __init__(self, handler: Callable[[str, dict, dict], Any]) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> HttpResponse:
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        result = self.handler(url, dict(params or {}), dict(headers or {}))
        if isinstance(result, HttpResponse):
            return result
        return HttpResponse(200, json.dumps(result, ensure_ascii=False), url=url)

    async def close(self) -> None:
        self.closed = True

    def paths(self) -> list[str]:
        return [urlparse(call["url"]).path for call in self.calls]
