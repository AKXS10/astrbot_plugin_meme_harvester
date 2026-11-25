"""解析后的字幕原文临时存储（按保留天数定期清理）。"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SAFE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_-]+")


def _safe_name(source_id: str) -> str:
    name = _SAFE.sub("_", source_id or "unknown").strip("_")
    return (name or "unknown")[:80]


class TempStore:
    """把每次解析得到的字幕/正文文本落一份到 ``temp/subtitles``。

    - 同一来源重复解析会覆盖同名文件，不会无限膨胀；
    - 清理只按文件修改时间判断，默认保留 3 天，可在设置里调整。
    """

    def __init__(self, root: Path, logger: Any) -> None:
        # logger 必须由 main.py 注入 AstrBot 的 logger；本模块不引入内置 logging，也不做任何回退。
        self.root = Path(root)
        self.dir = self.root / "subtitles"
        self.logger = logger

    def ensure(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, source_id: str, *, title: str = "", provider: str = "", text: str = "") -> Path:
        self.ensure()
        path = self.dir / f"{_safe_name(source_id)}.txt"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        header = f"# {title}\n# source={source_id} provider={provider} saved={stamp}\n\n"
        path.write_text(header + text, encoding="utf-8")
        return path

    def cleanup(self, days: int = 3) -> int:
        if not self.dir.is_dir():
            return 0
        cutoff = time.time() - max(1, int(days)) * 86400
        removed = 0
        for item in self.dir.glob("*.txt"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        if removed and self.logger is not None:
            self.logger.info("temp 清理完成，删除 %s 个过期文件（保留 %s 天）", removed, days)
        return removed

    def stats(self) -> dict[str, Any]:
        files = 0
        total = 0
        oldest = 0.0
        # 单趟遍历 + 逐个 try：清理线程可能刚好删掉某个文件，
        # 旧写法（exists() 后再 stat()）会抛 FileNotFoundError 让 /status 整体 500。
        for item in self.dir.glob("*.txt") if self.dir.is_dir() else ():
            try:
                info = item.stat()
            except OSError:
                continue
            files += 1
            total += info.st_size
            if not oldest or info.st_mtime < oldest:
                oldest = info.st_mtime
        return {
            "path": str(self.dir),
            "files": files,
            "bytes": total,
            "oldest": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(oldest)) if oldest else "",
        }
