"""合集合集/系列拉取（分页、限流由 client 负责）。"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from ..models import VideoInfo
from .client import BiliClient
from .urls import ParsedTarget


async def collect_target(
    client: BiliClient,
    target: ParsedTarget,
    *,
    page_size: int = 30,
    max_items: int = 500,
    on_page: Callable[[int, int, int], Awaitable[None]] | None = None,
) -> tuple[list[VideoInfo], int]:
    """返回 (已收集视频, 合集总数)。max_items 截断保护。"""
    if target.kind not in ("season", "series"):
        return [], 0

    collected: list[VideoInfo] = []
    seen: set[str] = set()
    total = 0
    page_size = max(1, min(int(page_size), 100))
    max_items = max(1, int(max_items))

    page = 1
    while len(collected) < max_items:
        if target.kind == "season":
            items, total = await client.get_season_archives(
                mid=target.mid, season_id=target.season_id, page_num=page, page_size=page_size
            )
        else:
            items, total = await client.get_series_archives(
                mid=target.mid, series_id=target.series_id, page_num=page, page_size=page_size
            )
        if not items:
            break
        for item in items:
            key = item.video_key
            if not item.bvid or key in seen:
                continue
            seen.add(key)
            collected.append(item)
            if len(collected) >= max_items:
                break
        if on_page is not None:
            await on_page(page, len(collected), total)
        if total and len(collected) >= min(total, max_items):
            break
        if len(items) < page_size:
            break
        page += 1
        if page > 200:  # 防御：异常分页
            break
        await asyncio.sleep(0)

    return collected, total or len(collected)
