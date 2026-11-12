"""插件装配与启动测试：用桩 Context 走一遍真实 Storage/TaskQueue/AuthManager 组装。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    import astrbot_plugin_meme_harvester.main as plugin_main  # noqa: E402
    from astrbot_plugin_meme_harvester.harvester import api as api_module  # noqa: E402

    HAS_ASTRBOT = True
except Exception:  # pragma: no cover - 纯 CI 环境没有 AstrBot
    plugin_main = None  # type: ignore[assignment]
    api_module = None  # type: ignore[assignment]
    HAS_ASTRBOT = False

from tests.test_api import FakeRequest, body  # noqa: E402


class StubContext:
    def __init__(self) -> None:
        self.web_apis: list[tuple] = []
        self.stars: dict = {}
        self.tools: list = []

    def register_web_api(self, route, handler, methods, desc=""):
        self.web_apis.append((route, handler, methods, desc))

    def get_all_providers(self):
        return []

    async def get_using_provider_async(self, umo=None):
        return None

    def get_registered_star(self, name):
        return self.stars.get(name)

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    async def llm_generate(self, **kwargs):
        raise AssertionError("测试不应调用真实 LLM")

    def get_registered_stars(self):
        return {}


@unittest.skipUnless(HAS_ASTRBOT, "需要 AstrBot 运行环境")
class PluginBootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self._original = plugin_main._data_dir
        plugin_main._data_dir = lambda: self.data_dir  # type: ignore[assignment]
        self.context = StubContext()
        self.plugin = plugin_main.MemeHarvesterPlugin(self.context, {})

    def tearDown(self) -> None:
        plugin_main._data_dir = self._original  # type: ignore[assignment]
        self.tmp.cleanup()

    def run_async(self, coro):
        return asyncio.run(coro)

    def test_registers_all_web_apis_with_prefix(self) -> None:
        routes = [item[0] for item in self.context.web_apis]
        self.assertGreaterEqual(len(routes), 20)
        for route in routes:
            self.assertTrue(route.startswith("/astrbot_plugin_meme_harvester/"))
        for _, handler, methods, _ in self.context.web_apis:
            self.assertTrue(callable(handler))
            self.assertTrue(methods)

    def test_lazy_start_opens_storage_and_queue(self) -> None:
        async def scenario():
            await self.plugin.ensure_started()
            self.assertTrue(self.plugin._ready)
            self.assertTrue(Path(self.plugin.storage.db_path).exists())
            stats = await self.plugin.storage.stats()
            return stats

        stats = self.run_async(scenario())
        self.assertEqual(stats["memes"], 0)
        self.run_async(self.plugin.terminate())

    def test_status_endpoint_after_boot(self) -> None:
        async def scenario():
            await self.plugin.ensure_started()
            api_module.request = FakeRequest()
            return await self.plugin.web_api.status()

        response = self.run_async(scenario())
        data = body(response)["data"]
        self.assertEqual(data["stats"]["videos"], 0)
        self.assertEqual(data["settings"]["review_required"], True)
        self.assertIn("auth", data)
        self.run_async(self.plugin.terminate())

    def test_settings_saved_to_db_survive_reload(self) -> None:
        async def scenario():
            await self.plugin.ensure_started()
            api_module.request = FakeRequest(body={"request_interval": "2.5", "inject_enabled": False})
            await self.plugin.web_api.settings_save()
            await self.plugin.reload_settings()
            return self.plugin.settings()

        settings = self.run_async(scenario())
        self.assertEqual(settings.request_interval, 2.5)
        self.assertFalse(settings.inject_enabled)
        self.run_async(self.plugin.terminate())

    def test_injection_hook_appends_system_prompt(self) -> None:
        class Event:
            unified_msg_origin = "group:1"

            def get_group_id(self):
                return "1"

            message_str = "这波操作太典了"

        class Req:
            prompt = "这波操作太典了"

            def __init__(self):
                self.system_prompt = "base"

        async def scenario():
            await self.plugin.ensure_started()
            await self.plugin.storage.upsert_slang("group:1", "典", "经典、名场面", [], "tester")
            request = Req()
            await self.plugin.inject_meme_context(Event(), request)
            return request.system_prompt

        prompt = self.run_async(scenario())
        self.assertIn("base", prompt)
        self.assertIn("【本群黑话与梗库参考】", prompt)
        self.assertIn("典", prompt)
        self.run_async(self.plugin.terminate())


if __name__ == "__main__":
    unittest.main()
