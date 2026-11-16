"""网页 / Wiki 抓取：SSRF 防护、MediaWiki API、HTML 正文提取。"""

from __future__ import annotations

import asyncio
import hashlib
import html as html_lib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from ..bili.client import BiliError
from ..bili.http import AiohttpTransport, HttpResponse
from ..constants import DEFAULT_UA, WIKI_HOSTS, ErrorCode
from ..models import Segment

_SKIP_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "button",
    "iframe",
    "select",
    "textarea",
    "canvas",
}
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "li",
    "ul",
    "ol",
    "td",
    "th",
    "tr",
    "table",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "dt",
    "dd",
    "br",
    "hr",
}
_VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "source", "track", "wbr"}
_CONTENT_MARKERS = (
    "mw-parser-output",
    "mw-content-text",
    "markdown-body",
    "article-content",
    "post-content",
    "entry-content",
)
_REF_RE = re.compile(r"\[\d{1,3}\]")
_SPACE_RE = re.compile(r"[ \t\u00a0\u3000]+")
_TITLE_SUFFIX_RE = re.compile(r"\s*[-_|]\s*(萌娘百科|维基百科|Wikipedia|Wiki|Fandom).*$", re.IGNORECASE)


@dataclass(slots=True)
class PageDocument:
    platform: str
    source_id: str
    url: str
    title: str = ""
    author: str = ""
    text: str = ""
    segments: list[Segment] = field(default_factory=list)
    provider: str = "web"
    lang: str = "zh"
    via: str = ""
    notes: list[str] = field(default_factory=list)


class SSRFGuard:
    """拒绝指向内网 / 本机 / 元数据地址的请求（尽力而为）。"""

    def __init__(self, resolver: Callable[[str], list[str]] | None = None) -> None:
        self._resolver = resolver or _resolve_host

    async def check(self, url: str) -> None:
        parsed = urlparse(url or "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BiliError(ErrorCode.BAD_URL, "仅支持 http/https 链接")
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise BiliError(ErrorCode.BAD_URL, "链接缺少域名")
        if host in {"localhost"} or host.endswith((".local", ".internal", ".localhost")):
            raise BiliError(ErrorCode.SSRF_BLOCKED, "不允许访问本机 / 内网域名")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if _blocked_ip(literal):
                raise BiliError(ErrorCode.SSRF_BLOCKED, "不允许访问内网地址")
            return

        try:
            addresses = await asyncio.to_thread(self._resolver, host)
        except OSError as exc:
            raise BiliError(ErrorCode.WEB_FETCH_FAILED, f"域名解析失败：{exc}") from exc
        if not addresses:
            raise BiliError(ErrorCode.WEB_FETCH_FAILED, "域名没有可用的 A/AAAA 记录")
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if _blocked_ip(ip):
                raise BiliError(ErrorCode.SSRF_BLOCKED, f"域名解析到内网地址（{address}）")


def _resolve_host(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return [info[4][0] for info in infos]


def _blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    return False


class _ReadableParser(HTMLParser):
    """从 HTML 中抽取可读正文，优先 ``article`` / MediaWiki 正文容器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self._capture = False
        self._capture_depth = 0
        self._saw_marker = False
        self._buffer: list[str] = []
        self._blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("article", "main") and not self._saw_marker:
            self._saw_marker = True
            self._start_capture()
        for name, value in attrs:
            if tag == "meta" and (name or "").lower() in ("property", "name"):
                content = dict(attrs).get("content") or ""
                if (value or "").lower() in ("og:title", "twitter:title") and content and not self.title:
                    self.title = content.strip()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        classes = " ".join(
            value or "" for name, value in attrs if (name or "").lower() == "class"
        ).lower()
        if classes and any(marker in classes for marker in _CONTENT_MARKERS) and not self._saw_marker:
            self._saw_marker = True
            self._start_capture()
        if self._capture and tag not in _VOID_TAGS:
            self._capture_depth += 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._flush()
        if self._capture:
            self._capture_depth -= 1
            if self._capture_depth <= 0:
                self._capture = False
                self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip_depth:
            return
        if not self._capture:
            if self._saw_marker:
                return
            self._capture = True
            self._capture_depth = 1
        self._buffer.append(data)

    def _start_capture(self) -> None:
        self._capture = True
        self._capture_depth = 1

    def _flush(self) -> None:
        text = "".join(self._buffer)
        self._buffer = []
        text = _SPACE_RE.sub(" ", text)
        text = text.replace("\r", " ").replace("\n", " ")
        text = _REF_RE.sub("", text).strip()
        if len(text) < 2:
            return
        if text in self._blocks[-1:]:
            return
        self._blocks.append(text)

    def result(self) -> tuple[str, list[str]]:
        self._flush()
        title = re.sub(r"\s+", " ", self.title).strip()
        return title, self._blocks


def extract_readable(html_text: str) -> tuple[str, list[str]]:
    """返回 (标题, 段落列表)。无法解析时返回空列表。"""
    parser = _ReadableParser()
    try:
        parser.feed(html_text or "")
        parser.close()
    except Exception:  # noqa: BLE001 - 容错：坏 HTML 不应让任务失败
        pass
    title, blocks = parser.result()
    title = html_lib.unescape(title)
    title = _TITLE_SUFFIX_RE.sub("", title).strip()
    cleaned: list[str] = []
    for block in blocks:
        block = re.sub(r"\s+", " ", block).strip()
        if len(block) < 2:
            continue
        cleaned.append(block)
    return title, cleaned


def text_to_segments(blocks: list[str], max_chars: int = 280, min_chars: int = 12) -> list[Segment]:
    """把网页段落切成与字幕等价的 Segment（无时间轴，start_ms=0）。"""
    segments: list[Segment] = []
    for block in blocks:
        block = block.strip()
        if not block or len(block) < min_chars:
            continue
        for piece in _split_long(block, max_chars):
            if len(piece) < min_chars:
                continue
            segments.append(
                Segment(index=len(segments), start_ms=0, end_ms=0, text=piece)
            )
    return segments


def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    buffer = ""
    for sentence in re.split(r"(?<=[。！？!?；;])", text):
        if not sentence:
            continue
        if len(buffer) + len(sentence) > limit and buffer:
            pieces.append(buffer)
            buffer = sentence
        else:
            buffer += sentence
    if buffer:
        pieces.append(buffer)
    return pieces


def looks_like_wiki(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return (
        any(host == d or host.endswith("." + d) for d in WIKI_HOSTS)
        or host.startswith("wiki.")
        or "/wiki/" in path
        or path.endswith("/api.php")
    )


def wiki_page_title(url: str) -> str:
    parsed = urlparse(url or "")
    path = unquote(parsed.path or "")
    if "/wiki/" in path:
        return path.split("/wiki/", 1)[1].split("#", 1)[0].replace("_", " ").strip()
    query = parse_qs(parsed.query)
    if query.get("title"):
        return (query.get("title") or [""])[0].strip()
    # 萌娘百科等直接以 /条目名 作为路径
    slug = path.lstrip("/").split("#", 1)[0]
    if slug and "/" not in slug and not slug.lower().endswith(".php"):
        return slug.replace("_", " ").strip()
    return ""


class WebPageResolver:
    """抓取网页并产出统一的 PageDocument。"""

    def __init__(
        self,
        transport: Any = None,
        settings_getter: Callable[[], Any] | None = None,
        *,
        guard: SSRFGuard | None = None,
        logger: Any = None,
    ) -> None:
        self.transport = transport or AiohttpTransport()
        self._settings = settings_getter or (lambda: None)
        self.guard = guard or SSRFGuard()
        self.logger = logger

    def _opt(self, name: str, default: Any) -> Any:
        settings = self._settings()
        return getattr(settings, name, default) if settings is not None else default

    async def fetch(self, url: str) -> PageDocument:
        await self.guard.check(url)
        platform = "wiki" if looks_like_wiki(url) else "web"
        doc: PageDocument | None = None
        if platform == "wiki":
            doc = await self._fetch_mediawiki(url)
        if doc is None:
            doc = await self._fetch_html(url, platform)
        min_chars = int(self._opt("web_min_chars", 200) or 0)
        if len(doc.text) < max(0, min_chars):
            raise BiliError(
                ErrorCode.WEB_CONTENT_EMPTY,
                f"正文只有 {len(doc.text)} 字，低于阈值 {min_chars}",
            )
        doc.source_id = _source_id(url)
        return doc

    async def _get(self, url: str, *, accept: str = "") -> HttpResponse:
        timeout = float(self._opt("web_timeout", 20) or 20)
        max_bytes = int(self._opt("web_max_bytes", 2_000_000) or 0)
        headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": accept or "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        }
        cookies = str(self._opt("web_cookies", "") or "")
        if cookies:
            headers["Cookie"] = cookies
        try:
            return await asyncio.wait_for(
                self.transport.get(url, headers=headers, max_bytes=max_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise BiliError(ErrorCode.WEB_FETCH_FAILED, "抓取超时") from exc
        except BiliError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise BiliError(ErrorCode.WEB_FETCH_FAILED, f"抓取失败：{exc}") from exc

    async def _fetch_mediawiki(self, url: str) -> PageDocument | None:
        parsed = urlparse(url)
        title = wiki_page_title(url)
        if not title:
            return None
        origin = f"{parsed.scheme}://{parsed.netloc}"
        params = {
            "action": "parse",
            "page": title,
            "prop": "text",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
            "disableeditsection": "1",
        }
        for api_path in ("/w/api.php", "/api.php"):
            api = f"{origin}{api_path}?{urlencode(params)}"
            try:
                resp = await self._get(api, accept="application/json")
            except BiliError:
                continue
            if resp.status != 200:
                continue
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or "error" in data:
                continue
            parse = data.get("parse") or {}
            html_text = parse.get("text")
            if isinstance(html_text, dict):
                html_text = html_text.get("*") or ""
            _, blocks = extract_readable(str(html_text or ""))
            text = "\n\n".join(blocks)
            if not text:
                continue
            max_chars = int(self._opt("max_source_chars", 60_000) or 60_000)
            text = text[:max_chars]
            page_title = str(parse.get("title") or title).strip()
            return PageDocument(
                platform="wiki",
                source_id="",
                url=url,
                title=page_title,
                text=text,
                segments=text_to_segments(blocks),
                provider="wiki",
                via=api_path.lstrip("/"),
            )
        return None

    async def _fetch_html(self, url: str, platform: str) -> PageDocument:
        resp = await self._get(url)
        if resp.status >= 400:
            raise BiliError(
                ErrorCode.WEB_FETCH_FAILED, f"HTTP {resp.status}（可能需要登录或站点拒绝抓取）"
            )
        title, blocks = extract_readable(resp.text)
        notes: list[str] = []
        if getattr(resp, "truncated", False):
            notes.append("页面超过大小上限，已截断")
        text = "\n\n".join(blocks)
        max_chars = int(self._opt("max_source_chars", 60_000) or 60_000)
        if len(text) > max_chars:
            text = text[:max_chars]
            notes.append(f"正文超过 {max_chars} 字，已截断")
        return PageDocument(
            platform=platform,
            source_id="",
            url=url,
            title=title or url,
            text=text,
            segments=text_to_segments(blocks),
            provider="wiki" if platform == "wiki" else "web",
            via="html",
            notes=notes,
        )


def _source_id(url: str) -> str:
    digest = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]
    return f"web:{digest}"
