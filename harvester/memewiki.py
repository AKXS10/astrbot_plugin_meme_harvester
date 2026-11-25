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
from typing import Any

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

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            try:
                getattr(self._logger, level, self._logger.info)(message)
            except Exception:
                pass

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
        skipped_ids: list[int] = []
        skipped_reason: list[str] = []
        existed_ids: list[int] = []
        added: dict[str, int] = {}  # term -> meme_id

        # 判重与写入放进同一个梗百科锁，保证两步的原子性：
        # 先逐一校验、经 setdefault 登记新增项，成功后一次性落盘
        async with self._lock:
            async with plugin._terms_lock:
                existing_norm = {
                    normalize_term(str(name)) for name in plugin._user_terms
                }
                for meme in pending:
                    meme_id = int(meme.get("id") or 0)
                    term = str(meme.get("term") or "").strip()
                    meaning = str(meme.get("definition") or "").strip()
                    if not term or len(term) > MEMEWIKI_MAX_TERM:
                        skipped_ids.append(meme_id)
                        skipped_reason.append("term_too_long")
                        continue
                    if not meaning:
                        skipped_ids.append(meme_id)
                        skipped_reason.append("empty_definition")
                        continue
                    norm_key = normalize_term(term)
                    if term in plugin._user_terms or norm_key in existing_norm:
                        existed_ids.append(meme_id)
                        continue
                    if dry_run:
                        # 干跑：不动内存、不落盘，只统计"将会新增"
                        existing_norm.add(norm_key)
                        added[term] = meme_id
                        continue
                    entry = {
                        "meaning": truncate(meaning, MEMEWIKI_MAX_MEANING),
                        "author": "meme_harvester",
                        "author_name": "梗采集器",
                        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    # setdefault：键已存在时不覆盖既有内容
                    if plugin._user_terms.setdefault(term, entry) is entry:
                        existing_norm.add(norm_key)
                        added[term] = meme_id
                    else:
                        # 并发写入撞上的同名词条：不覆盖，记为已存在
                        existed_ids.append(meme_id)

                if not dry_run and added:
                    try:
                        await asyncio.to_thread(plugin._save_user_terms)
                    except Exception as exc:  # noqa: BLE001
                        # 落盘失败：回滚内存新增，让内存和文件保持一致
                        for name in added:
                            plugin._user_terms.pop(name, None)
                        result.failed = len(added)
                        result.message = f"写入梗百科失败: {exc}"
                        await self.storage.add_log("error", "memewiki_sync", result.message)
                        return result

            for name in added:
                try:
                    plugin._invalidate_term_cache(name)
                except Exception:  # noqa: BLE001
                    pass

            result.existed = len(existed_ids)
            result.skipped = len(skipped_ids)
            result.synced = len(added)

            if not dry_run:
                added_ids = list(added.values())
                if added_ids:
                    await self.storage.mark_synced(added_ids, "synced", slug="memewiki:custom")
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

    async def remove_term(self, name: str) -> bool:
        """词条改名 / 删除时，把梗百科里「本插件写入的同名旧词条」一并移除。

        只删 author == "meme_harvester" 的条目：用户在梗百科 WebUI 手动建的
        同名黑话不动。返回是否实际删除。
        """
        name = str(name or "").strip()
        if not name:
            return False
        probe = self.probe()
        if probe.mode != MODE_INPROCESS or probe.plugin is None:
            return False
        plugin = probe.plugin
        removed = False
        try:
            async with self._lock:
                async with plugin._terms_lock:
                    entry = plugin._user_terms.get(name)
                    if not isinstance(entry, dict):
                        return False
                    if str(entry.get("author") or "") != "meme_harvester":
                        return False
                    plugin._user_terms.pop(name, None)
                    removed = True
                    try:
                        await asyncio.to_thread(plugin._save_user_terms)
                    except Exception:
                        # 落盘失败：回滚内存删除，保持内存与文件一致（与 sync() 同策略）
                        plugin._user_terms[name] = entry
                        raise
            if removed:
                try:
                    plugin._invalidate_term_cache(name)
                except Exception:  # noqa: BLE001
                    pass
                await self.storage.add_log(
                    "info", "memewiki_sync", f"词条改名，已移除梗百科旧条目：{name}"
                )
        except Exception as exc:  # noqa: BLE001
            await self.storage.add_log(
                "warn", "memewiki_sync", f"移除梗百科旧条目失败（{name}）: {exc}"
            )
            return False
        return removed

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
