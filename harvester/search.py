"""检索层：把「用户这句话里出现的黑话 / 梗」拼成可注入的提示块。"""

from __future__ import annotations

from typing import Any, Callable

from .storage import Storage
from .text import truncate

INJECT_HEADER = "【本群黑话与梗库参考】"
_MAX_ITEM_CHARS = 110


class MemeSearch:
    def __init__(self, storage: Storage, settings_getter: Callable[[], Any]) -> None:
        self.storage = storage
        self._settings = settings_getter

    async def lookup(
        self, text: str, *, scope: str = "", limit: int = 4
    ) -> list[dict[str, Any]]:
        """返回命中的黑话/词条，群黑话优先，其次是已发布梗库。"""
        text = (text or "").strip()
        if not text:
            return []
        found: list[dict[str, Any]] = []
        seen: set[str] = set()

        if scope:
            for row in await self.storage.find_slangs(scope, text, limit=limit):
                term = str(row.get("term") or "")
                if not term or term in seen:
                    continue
                seen.add(term)
                found.append(
                    {
                        "source": "slang",
                        "term": term,
                        "definition": str(row.get("definition") or ""),
                        "aliases": _as_list(row.get("aliases")),
                    }
                )
                if len(found) >= limit:
                    return found

        for row in await self.storage.match_memes_in_text(text, limit=limit):
            term = str(row.get("term") or "")
            if not term or term in seen:
                continue
            seen.add(term)
            found.append(
                {
                    "source": "meme",
                    "term": term,
                    "definition": str(row.get("definition") or ""),
                    "aliases": _as_list(row.get("aliases")),
                }
            )
            if len(found) >= limit:
                break
        return found

    async def build_injection(
        self, text: str, *, scope: str = "", max_chars: int | None = None
    ) -> str:
        settings = self._settings()
        if not getattr(settings, "inject_enabled", True):
            return ""
        limit = max_chars or int(getattr(settings, "inject_max_chars", 600) or 600)
        entries = await self.lookup(text, scope=scope, limit=4)
        if not entries:
            return ""
        lines = [INJECT_HEADER]
        used = len(INJECT_HEADER) + 2
        for entry in entries:
            line = f"- {entry['term']}：{truncate(entry['definition'], _MAX_ITEM_CHARS)}"
            if entry["aliases"]:
                line += f"（别名：{'、'.join(entry['aliases'][:3])}）"
            if used + len(line) + 1 > limit:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines) if len(lines) > 1 else ""

    async def describe(self, term: str, *, scope: str = "") -> str:
        """给 /梗 命令用：返回格式化的释义文本。"""
        term = (term or "").strip()
        if not term:
            return ""
        if scope:
            slangs = await self.storage.find_slangs(scope, term, limit=3)
            if slangs:
                row = slangs[0]
                return f"{row.get('term')}：{row.get('definition') or ''}"
        meme = await self.storage.find_meme(term)
        if meme is None:
            hits = await self.storage.list_memes(term, limit=3)
            meme = hits[0] if hits else None
        if meme is None:
            return ""
        parts = [f"{meme['term']}：{meme.get('definition') or ''}"]
        if meme.get("usage"):
            parts.append(f"用法：{meme['usage']}")
        if meme.get("origin"):
            parts.append(f"出处：{meme['origin']}")
        return "\n".join(parts)


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    if isinstance(value, str) and value.strip():
        return [v.strip() for v in value.replace("，", ",").split(",") if v.strip()]
    return []
