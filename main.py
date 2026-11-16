"""梗采集器：从 B 站字幕抽取网络梗，落本地库并同步到「梗百科」。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .harvester import __version__
from .harvester.api import WebApi
from .harvester.auth import AuthManager
from .harvester.bili.client import BiliClient, BiliError
from .harvester.bili.http import AiohttpTransport
from .harvester.bili.urls import parse_target
from .harvester.config import Settings
from .harvester.constants import DB_FILENAME, ERROR_MESSAGES, PLUGIN_NAME, ErrorCode
from .harvester.extractor import MemeExtractor
from .harvester.llm import LLMService
from .harvester.memewiki import MemewikiAdapter
from .harvester.pipeline import VideoPipeline, VideoResult
from .harvester.search import MemeSearch
from .harvester.sources.asr import Transcriber
from .harvester.sources.classify import classify_url
from .harvester.sources.media import MediaResolver
from .harvester.sources.webpage import WebPageResolver
from .harvester.storage import Storage
from .harvester.tasks import TaskQueue

try:  # 4.9.2+ 提供 StarTools
    from astrbot.core.star.star_tools import StarTools
except Exception:  # pragma: no cover
    StarTools = None  # type: ignore[assignment]


def _data_dir() -> Path:
    if StarTools is not None:
        try:
            return StarTools.get_data_dir(PLUGIN_NAME)
        except Exception:  # pragma: no cover - 直接实例化（测试）
            pass
    base = Path.home() / ".astrbot" / "data" / "plugin_data"
    path = base / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


@register(
    PLUGIN_NAME,
    "MemeHarvester",
    "从 B 站字幕自动抽取网络梗，支持合集批量解析、WebUI 管理与梗百科同步",
    __version__,
    "https://github.com/your-name/astrbot_plugin_meme_harvester",
)
class MemeHarvesterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.raw_config = dict(config or {})
        self.version = __version__
        self.data_dir = _data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.storage = Storage(self.data_dir / DB_FILENAME)
        self.client = BiliClient(AiohttpTransport())
        self.llm = LLMService(context, self._get_settings)
        self.extractor = MemeExtractor(self.llm.complete_json)
        self.transcriber = Transcriber(self._get_settings, logger=logger)
        self.media = MediaResolver(
            AiohttpTransport(), self._get_settings, transcriber=self.transcriber, logger=logger
        )
        self.web = WebPageResolver(AiohttpTransport(), self._get_settings, logger=logger)
        self.pipeline = VideoPipeline(
            self.client,
            self.storage,
            self.extractor,
            self._get_settings,
            logger=logger,
            media_resolver=self.media,
            web_resolver=self.web,
            tmp_root=self.data_dir / "tmp",
        )
        self.queue = TaskQueue(
            self.storage,
            self.pipeline,
            self._get_settings,
            sync_fn=self._sync_memewiki,
            logger=logger,
        )
        self.auth = AuthManager(self.storage, self.client, self.data_dir, logger=logger)
        self.memewiki = MemewikiAdapter(context, self.storage, self.data_dir, logger=logger)
        self.search = MemeSearch(self.storage, self._get_settings)
        self.web_api = WebApi(self)

        self._settings = Settings.from_config(self.raw_config)
        self._ready = False
        self._start_lock = asyncio.Lock()

        self.web_api.register(context)

    # ------------------------------------------------------------ 生命周期

    def _get_settings(self) -> Settings:
        return self._settings

    def settings(self) -> Settings:
        return self._settings

    async def ensure_started(self) -> None:
        """首个请求 / 事件到达时懒初始化（Star 的构造函数是同步的）。"""
        if self._ready:
            return
        async with self._start_lock:
            if self._ready:
                return
            await self.storage.open()
            await self.reload_settings()
            await self.auth.restore()
            await self.queue.start()
            self._ready = True
            logger.info("[meme_harvester] 初始化完成，数据目录 %s", self.data_dir)

    async def reload_settings(self) -> Settings:
        overrides = await self.storage.get_settings()
        merged = Settings.from_config(self.raw_config).merge(overrides)
        self._settings = merged
        interval = float(merged.request_interval)
        self.client.interval = interval
        self.client.risk_cooldown = 1800.0
        return merged

    async def terminate(self) -> None:
        try:
            if self._ready:
                await self.queue.shutdown()
                await self.client.close()
                await self.storage.close()
        except Exception:  # noqa: BLE001
            logger.exception("[meme_harvester] 关闭时出错")

    # ------------------------------------------------------------ 业务封装

    async def _sync_memewiki(self):
        return await self.memewiki.sync(limit=200)

    async def parse_video(self, bvid: str, page: int = 1, force: bool = False) -> VideoResult:
        await self.ensure_started()
        return await self.pipeline.process_video(bvid, page=page, force=force)

    # ------------------------------------------------------------ 提示词注入

    @filter.on_llm_request()
    async def inject_meme_context(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        try:
            await self.ensure_started()
        except Exception:  # noqa: BLE001
            return
        if not self._settings.inject_enabled:
            return
        text = (req.prompt or getattr(event, "message_str", "") or "").strip()
        if not text:
            return
        scope = ""
        try:
            group_id = event.get_group_id()
            if group_id:
                scope = f"group:{group_id}"
        except Exception:  # noqa: BLE001
            scope = ""
        try:
            block = await self.search.build_injection(
                text, scope=scope, max_chars=int(self._settings.inject_max_chars)
            )
        except Exception:  # noqa: BLE001
            return
        if block:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block

    # ------------------------------------------------------------ 命令

    @filter.command("梗解析")
    async def cmd_parse(self, event: AstrMessageEvent, url: str = "") -> None:
        """解析视频 / 网页并抽取梗：/梗解析 <链接>"""
        raw = (url or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：/梗解析 <B 站视频/BV 号、其它平台视频链接、百科网页链接>\n"
                "合集请在插件 WebUI 的「合集解析」页提交。"
            )
            return
        route = classify_url(raw)
        if route.kind == "unsupported":
            yield event.plain_result(route.reason or "无法识别的链接。")
            return
        if route.kind in ("season", "series"):
            yield event.plain_result("这是合集/系列链接，请到插件 WebUI 的「合集解析」页提交。")
            return
        yield event.plain_result("开始解析，视频或页面较长时需要几十秒到几分钟…")
        try:
            await self.ensure_started()
            result = await self.pipeline.process_url(raw)
        except BiliError as exc:
            hint = ERROR_MESSAGES.get(exc.code, "")
            yield event.plain_result(f"解析失败：{hint or exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[meme_harvester] 解析失败")
            yield event.plain_result(f"解析失败：{exc}")
            return
        yield event.plain_result(self._format_result(result))

    @filter.command("梗查")
    async def cmd_lookup(self, event: AstrMessageEvent, term: str = "") -> None:
        """查询词条：/梗查 <词>"""
        keyword = (term or "").strip()
        if not keyword:
            yield event.plain_result("用法：/梗查 <梗名称>")
            return
        await self.ensure_started()
        text = await self.search.describe(keyword)
        yield event.plain_result(text)

    @filter.command("梗库")
    async def cmd_stats(self, event: AstrMessageEvent) -> None:
        """查看本地梗库统计。"""
        await self.ensure_started()
        stats = await self.storage.stats()
        yield event.plain_result(
            f"梗库：{stats.get('memes', 0)} 条词条，{stats.get('videos', 0)} 个视频，"
            f"待审核 {stats.get('reviews', 0)} 条，群黑话 {stats.get('slangs', 0)} 条。"
        )

    @filter.command("梗同步")
    async def cmd_sync(self, event: AstrMessageEvent) -> None:
        """把待同步词条写入「梗百科」。"""
        await self.ensure_started()
        try:
            result = await self.memewiki.sync(limit=200)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"同步失败：{exc}")
            return
        mode = getattr(result, "mode", "")
        if mode == "unavailable":
            yield event.plain_result("未检测到「梗百科」插件，已跳过。可在 WebUI 导出 JSONL 手动导入。")
            return
        yield event.plain_result(
            f"同步完成：新增 {getattr(result, 'synced', 0)}，已存在 {getattr(result, 'existed', 0)}，"
            f"跳过 {getattr(result, 'skipped', 0)}（模式 {mode}）。"
        )

    # ------------------------------------------------------------ 工具

    @staticmethod
    def _format_result(result: VideoResult) -> str:
        if result.status == "skipped":
            return f"已解析过：{result.title or result.video_key}（跳过）"
        if result.status == "failed":
            try:
                hint = ERROR_MESSAGES.get(ErrorCode(result.error), "")
            except ValueError:
                hint = ""
            return f"解析失败：{hint or result.error}"
        label = result.bvid or result.video_key
        lines = [
            f"《{result.title or label}》解析完成",
            f"来源：{result.platform or 'bilibili'}｜文本轨：{result.track_lan or '-'}（{result.provider}）"
            f"｜分块 {result.chunks}",
            f"抽取 {result.memes_total} 条，新增 {result.memes_created} 条，"
            f"冲突 {result.conflicts}，待审核 {result.reviews}",
        ]
        if result.provider.startswith("ai"):
            lines.append("提示：该视频只有 AI 字幕，个别专有名词可能有识别误差。")
        elif result.provider == "whisper":
            lines.append("提示：本地转写可能有错字，专有名词建议人工核对。")
        return "\n".join(lines)
