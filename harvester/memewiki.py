"""梗百科（memewiki）进程内适配。

设计约束：
- 只增不改：本地已有同名自定义黑话时只记 existed，不覆盖用户内容。
- 梗百科 1.6.0 的可写层是 ``_user_terms`` + ``_save_user_terms()``，
  写入后必须调用 ``_invalidate_term_cache(name)``，否则 10 分钟未命中缓存
  会让新词条查不到。
- 找不到插件或结构不匹配时降级为导出 JSONL，数据先留在本插件词库。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .constants import MEMEWIKI_MAX_MEANING, MEMEWIKI_MAX_TERM, MEMEWIKI_PLUGIN_NAME
from .models import SyncResult
from .storage import Storage
from .text import normalize_term, truncate

MODE_INPROCESS = "inprocess"
MODE_EXPORT = "export"
MODE_UNAVAILABLE = "unavailable"

_REQUIRED_ATTRS = ("_user_terms", "_save_user_terms", "_invalidate_term_cache", "_terms_lock")


@dataclass(slots=True)
class Probe:
    """适配器能力探测结果。plugin 只在进程内可用，不参与序列化。"""

    available: bool = False
    mode: str = MODE_UNAVAILABLE
    reason: str = ""
    terms: int = 0
    plugin: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "mode": self.mode,
            "reason": self.reason,
            "terms": self.terms,
        }


class MemewikiAdapter:
    def __init__(
        self,
        context: Any,
        storage: Storage,
        data_dir: Path | str,
        *,
        logger: Any = None,
    ) -> None:
        self.context = context
        self.storage = storage
        self.data_dir = Path(data_dir)
        self._logger = logger
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 探测

    def probe(self) -> Probe:
        getter = getattr(self.context, "get_registered_star", None)
        if not callable(getter):
            return Probe(reason="当前 AstrBot 不支持 get_registered_star")
        try:
            meta = getter(MEMEWIKI_PLUGIN_NAME)
        except Exception as exc:  # noqa: BLE001
            return Probe(reason=f"查询梗百科插件失败: {exc}")
        if meta is None:
            return Probe(reason="未安装梗百科（astrbot_plugin_memewiki）")
        plugin = getattr(meta, "star_cls", None)
        if plugin is None:
            return Probe(reason="梗百科插件未加载完成")
        missing = [name for name in _REQUIRED_ATTRS if not hasattr(plugin, name)]
        if missing:
            return Probe(
                mode=MODE_EXPORT,
                reason=f"梗百科版本不匹配，缺少 {', '.join(missing)}",
                plugin=plugin,
            )
        terms = getattr(plugin, "_user_terms", {}) or {}
        return Probe(
            available=True,
            mode=MODE_INPROCESS,
            reason="",
            terms=len(terms),
            plugin=plugin,
        )

    # ------------------------------------------------------------------ 同步

    async def sync(self, limit: int = 200, *, dry_run: bool = False) -> SyncResult:
        probe = self.probe()
        pending = await self.storage.pending_sync_memes(limit=limit)
        result = SyncResult(scanned=len(pending), mode=probe.mode)
        if not pending:
            result.message = "没有待同步词条"
            return result
        if probe.mode != MODE_INPROCESS or probe.plugin is None:
            result.skipped = len(pending)
            result.message = probe.reason or "梗百科不可用"
            return result

        plugin = probe.plugin
        entries: dict[str, dict[str, Any]] = {}
        skipped_ids: list[int] = []
        skipped_reason: list[str] = []
        existed_ids: list[int] = []

        async with self._lock:
            existing_norm = {normalize_term(str(name)) for name in plugin._user_terms}
            for meme in pending:
                term = str(meme.get("term") or "").strip()
                meaning = str(meme.get("definition") or "").strip()
                if not term or len(term) > MEMEWIKI_MAX_TERM:
                    skipped_ids.append(int(meme["id"]))
                    skipped_reason.append("term_too_long")
                    continue
                if not meaning:
                    skipped_ids.append(int(meme["id"]))
                    skipped_reason.append("empty_definition")
                    continue
                if term in plugin._user_terms or normalize_term(term) in existing_norm:
                    existed_ids.append(int(meme["id"]))
                    continue
                entries[term] = {
                    "meaning": truncate(meaning, MEMEWIKI_MAX_MEANING),
                    "author": "meme_harvester",
                    "author_name": "梗采集器",
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                }

            result.existed = len(existed_ids)
            result.skipped = len(skipped_ids)

            if entries and not dry_run:
                try:
                    async with plugin._terms_lock:
                        plugin._user_terms.update(entries)
                        await asyncio.to_thread(plugin._save_user_terms)
                except Exception as exc:  # noqa: BLE001
                    result.failed = len(entries)
                    result.message = f"写入梗百科失败: {exc}"
                    await self.storage.add_log("error", "memewiki_sync", result.message)
                    return result
                for name in entries:
                    try:
                        plugin._invalidate_term_cache(name)
                    except Exception:  # noqa: BLE001
                        pass

            result.synced = len(entries)
            synced_ids = [int(m["id"]) for m in pending if str(m.get("term") or "").strip() in entries]
            if not dry_run:
                if synced_ids:
                    await self.storage.mark_synced(synced_ids, "synced", slug="memewiki:custom")
                if existed_ids:
                    await self.storage.mark_synced(existed_ids, "existed", slug="memewiki:custom")
                if skipped_ids:
                    reason = ",".join(sorted(set(skipped_reason))) or "skipped"
                    await self.storage.mark_synced(skipped_ids, "skipped", error=reason)
                await self.storage.add_log(
                    "info",
                    "memewiki_sync",
                    f"同步完成：新增 {result.synced}，已存在 {result.existed}，跳过 {result.skipped}",
                )
            result.message = f"新增 {result.synced} 条，已存在 {result.existed} 条，跳过 {result.skipped} 条"
            return result

    # ------------------------------------------------------------ 导出兜底

    async def export_jsonl(self, limit: int = 500) -> Path:
        """把待同步词条导出成 JSONL，供手动导入或排查。返回文件路径。"""
        pending = await self.storage.pending_sync_memes(limit=limit)
        if not pending:
            pending = await self.storage.list_memes(status="published", limit=limit)
        out_dir = self.data_dir / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"memewiki-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        lines: list[str] = []
        for meme in pending:
            term = str(meme.get("term") or "").strip()
            meaning = str(meme.get("definition") or "").strip()
            if not term or not meaning:
                continue
            lines.append(
                json.dumps(
                    {
                        "name": term,
                        "meaning": truncate(meaning, MEMEWIKI_MAX_MEANING),
                        "author": "meme_harvester",
                        "author_name": "梗采集器",
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    ensure_ascii=False,
                )
            )
        await asyncio.to_thread(path.write_text, "\n".join(lines) + ("\n" if lines else ""), "utf-8")
        return path

    async def status(self) -> dict[str, Any]:
        probe = self.probe()
        pending = await self.storage.pending_sync_memes(limit=500)
        return {**probe.to_dict(), "pending": len(pending)}
