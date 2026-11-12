"""Cookie 解析与合并（不落日志、不回显）。"""

from __future__ import annotations

from http.cookies import SimpleCookie


def parse_cookie_string(raw: str) -> dict[str, str]:
    """支持 `a=1; b=2` 与浏览器插件导出的 JSON 片段。"""
    jar: dict[str, str] = {}
    text = (raw or "").strip()
    if not text:
        return jar
    if text.startswith("{"):
        import json

        try:
            payload = json.loads(text)
        except ValueError:
            return jar
        if isinstance(payload, dict):
            for key, value in payload.items():
                if isinstance(value, str):
                    jar[str(key)] = value
        return jar
    for part in text.replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        if key:
            jar[key] = value
    return jar


def merge_set_cookies(jar: dict[str, str], set_cookie_headers: list[str]) -> None:
    for header in set_cookie_headers or []:
        cookie = SimpleCookie()
        try:
            cookie.load(header)
        except Exception:
            continue
        for key, morsel in cookie.items():
            value = morsel.value
            if value in ("", "deleted"):
                jar.pop(key, None)
            else:
                jar[key] = value


def cookie_header(jar: dict[str, str], names: list[str] | None = None) -> str:
    if not jar:
        return ""
    keys = names if names is not None else list(jar.keys())
    return "; ".join(f"{k}={jar[k]}" for k in keys if jar.get(k))


def mask_cookie(jar: dict[str, str]) -> dict[str, str]:
    masked: dict[str, str] = {}
    for key, value in jar.items():
        masked[key] = (value[:3] + "***") if len(value) > 6 else "***"
    return masked
