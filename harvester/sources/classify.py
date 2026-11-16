"""链接分类：B 站 / 其它视频 / Wiki / 普通网页。"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ..bili.urls import parse_target
from ..constants import MEDIA_EXTENSIONS, MEDIA_HOSTS, WIKI_HOSTS


@dataclass(slots=True)
class SourceRoute:
    kind: str  # bilibili / media / wiki / web / unsupported
    url: str
    platform: str = ""
    reason: str = ""

    @property
    def supported(self) -> bool:
        return self.kind != "unsupported"


def normalize_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text
    return text


def _host_matches(host: str, domains: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_url(raw: str) -> SourceRoute:
    text = (raw or "").strip()
    if not text:
        return SourceRoute("unsupported", "", reason="空链接")

    # 纯 BV / av 号直接交给 B 站链路
    if "://" not in text:
        bili = parse_target(text)
        if bili.kind in ("video", "short", "season", "series"):
            return SourceRoute("bilibili", text, platform="bilibili")

    url = normalize_url(text)
    try:
        parsed = urlparse(url)
    except ValueError:
        return SourceRoute("unsupported", url, reason="链接格式错误")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return SourceRoute("unsupported", url, reason="仅支持 http/https 链接")

    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    path = (parsed.path or "").lower()
    if not host or ("." not in host and host != "localhost"):
        return SourceRoute("unsupported", url, reason="无法识别的域名")

    bili = parse_target(url)
    if bili.kind in ("video", "short", "season", "series"):
        return SourceRoute("bilibili", url, platform="bilibili")

    if _host_matches(host, MEDIA_HOSTS) or path.endswith(MEDIA_EXTENSIONS):
        return SourceRoute("media", url, platform=_media_platform(host, path))

    if (
        _host_matches(host, WIKI_HOSTS)
        or host.startswith("wiki.")
        or "/wiki/" in path
        or path.endswith("/api.php")
    ):
        return SourceRoute("wiki", url, platform="wiki")

    return SourceRoute("web", url, platform="web")


def _media_platform(host: str, path: str) -> str:
    if host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if host.endswith(("douyin.com", "iesdouyin.com")):
        return "douyin"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith(("x.com", "twitter.com", "t.co")):
        return "x"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith(("facebook.com", "fb.watch")):
        return "facebook"
    if host.endswith("vimeo.com"):
        return "vimeo"
    if host.endswith("youku.com"):
        return "youku"
    if host.endswith("iqiyi.com"):
        return "iqiyi"
    if host.endswith("v.qq.com"):
        return "tencent"
    if host.endswith(("weibo.com", "weibo.cn", "t.cn")):
        return "weibo"
    if host.endswith(("xiaohongshu.com", "xhslink.com")):
        return "xiaohongshu"
    if host.endswith("soundcloud.com"):
        return "soundcloud"
    if host.endswith(("podcasts.apple.com", "open.spotify.com")):
        return "podcast"
    if host.endswith("bilibili.tv"):
        return "bilibili_tv"
    if path.endswith(MEDIA_EXTENSIONS):
        return "direct"
    return "video"
