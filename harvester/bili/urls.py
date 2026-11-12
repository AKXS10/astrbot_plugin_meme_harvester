"""B 站链接解析：视频 / 短链 / 合集 / 系列。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:^|/|video/)av(\d+)", re.IGNORECASE)
_SPACE_RE = re.compile(r"^/(\d+)/")
_SID_RE = re.compile(r"[?&]sid=(\d+)")

ALLOWED_HOSTS = {
    "bilibili.com",
    "www.bilibili.com",
    "m.bilibili.com",
    "space.bilibili.com",
    "b23.tv",
    "bili2233.cn",
}


@dataclass(slots=True)
class ParsedTarget:
    kind: str  # video / season / series / short / unsupported
    raw: str
    bvid: str = ""
    aid: int = 0
    page: int = 1
    mid: int = 0
    season_id: int = 0
    series_id: int = 0
    reason: str = ""


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def parse_target(text: str) -> ParsedTarget:
    raw = (text or "").strip()
    if not raw:
        return ParsedTarget(kind="unsupported", raw=raw, reason="空链接")

    # 纯 BV / av 号
    bv = _BV_RE.search(raw)
    if bv and "://" not in raw:
        return ParsedTarget(kind="video", raw=raw, bvid=bv.group(1))
    av = _AV_RE.search(raw)
    if av and "://" not in raw:
        return ParsedTarget(kind="video", raw=raw, aid=int(av.group(1)))

    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw

    try:
        parsed = urlparse(raw)
    except ValueError:
        return ParsedTarget(kind="unsupported", raw=raw, reason="链接格式错误")

    host = parsed.netloc.split(":")[0]
    if not _host_allowed(host):
        return ParsedTarget(kind="unsupported", raw=raw, reason="不是 B 站域名")

    if host.endswith("b23.tv") or host.endswith("bili2233.cn"):
        return ParsedTarget(kind="short", raw=raw)

    query = parse_qs(parsed.query)
    page = _int(query.get("p", ["1"])[0], 1)
    sid = _int(query.get("sid", ["0"])[0], 0)

    space_match = _SPACE_RE.match(parsed.path or "")
    mid = int(space_match.group(1)) if space_match else 0

    bv = _BV_RE.search(parsed.path) or _BV_RE.search(parsed.query)
    if bv:
        return ParsedTarget(kind="video", raw=raw, bvid=bv.group(1), page=max(1, page))
    av = _AV_RE.search(parsed.path)
    if av:
        return ParsedTarget(kind="video", raw=raw, aid=int(av.group(1)), page=max(1, page))

    if "/channel/collectiondetail" in parsed.path and mid and sid:
        return ParsedTarget(kind="season", raw=raw, mid=mid, season_id=sid)
    if "/channel/seriesdetail" in parsed.path and mid and sid:
        return ParsedTarget(kind="series", raw=raw, mid=mid, series_id=sid)
    if "/video" in parsed.path and mid and sid:
        # space 的视频页带 sid 时也当作合集
        return ParsedTarget(kind="season", raw=raw, mid=mid, season_id=sid)

    return ParsedTarget(kind="unsupported", raw=raw, reason="暂不支持该页面类型")


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
