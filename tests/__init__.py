"""测试包初始化：把 AstrBot 的根目录重定向到仓库内 .tmp/，避免测试污染当前目录。

AstrBot 的 ``get_astrbot_root()`` 默认取当前工作目录，导入其配置模块时会在
``./data`` 下创建模板文件。测试只需要一个可丢弃的根目录。
"""

from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT = _REPO_ROOT / ".tmp" / "astrbot-root"
_TEST_ROOT.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("ASTRBOT_ROOT", str(_TEST_ROOT))
