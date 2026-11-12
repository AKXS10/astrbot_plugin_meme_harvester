"""WBI 签名。算法参考 bilibili-API-collect。"""

from __future__ import annotations

import hashlib
import time
from typing import Any
from urllib.parse import urlencode

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]

_FORBIDDEN = "!'()*"


def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]


def sign_params(params: dict[str, Any], img_key: str, sub_key: str, wts: int | None = None) -> dict[str, str]:
    """返回带 wts / w_rid 的字符串参数（值按 B 站规则过滤）。"""
    key = mixin_key(img_key, sub_key)
    timestamp = int(wts if wts is not None else time.time())
    merged: dict[str, str] = {}
    for name, value in params.items():
        if value is None:
            continue
        text = str(value)
        merged[name] = "".join(ch for ch in text if ch not in _FORBIDDEN)
    merged["wts"] = str(timestamp)
    query = urlencode(sorted(merged.items()))
    merged["w_rid"] = hashlib.md5((query + key).encode("utf-8")).hexdigest()
    return merged
