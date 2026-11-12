"""B 站采集子包。"""

from .client import BiliClient, BiliError
from .collection import collect_target
from .cookies import cookie_header, mask_cookie, merge_set_cookies, parse_cookie_string
from .http import AiohttpTransport, HttpResponse
from .subtitle import (
    merge_segments,
    parse_subtitle_payload,
    pick_track,
    provider_label,
    rank_tracks,
    segments_text_hash,
    track_kind,
)
from .urls import ParsedTarget, parse_target

__all__ = [
    "AiohttpTransport",
    "BiliClient",
    "BiliError",
    "HttpResponse",
    "ParsedTarget",
    "collect_target",
    "cookie_header",
    "mask_cookie",
    "merge_segments",
    "merge_set_cookies",
    "parse_cookie_string",
    "parse_subtitle_payload",
    "parse_target",
    "pick_track",
    "provider_label",
    "rank_tracks",
    "segments_text_hash",
    "track_kind",
]
