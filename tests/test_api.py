"""WebAPI 处理器测试：不启动 AstrBot，直接以假 request/plugin 调用。"""

from __future__ import annotations

import asyncio
import json
import unittest

try:
    from harvester import api as api_module
    from harvester.api import WebApi
    from harvester.config import Settings

    HAS_ASTRBOT = True
except Exception:  # pragma: no cover - 纯 CI 环境没有 AstrBot
    api_module = None  # type: ignore[assignment]
    WebApi = None  # type: ignore[assignment]
    Settings = None  # type: ignore[assignment]
    HAS_ASTRBOT = False


class FakeQuery:
    def __init__(self, values: dict | None = None) -> None:
        self.values = values or {}

    def get(self, key, default=None, type=None):
        value = self.values.get(key, default)
        if value is None:
            return default
        if type is not None:
            try:
                return type(value)
            except (TypeError, ValueError):
                return default
        return value


class FakeRequest:
    def __init__(self, query: dict | None = None, body: dict | None = None) -> None:
        self.query = FakeQuery(query)
        self._body = body or {}

    async def json(self, default=None):
        return dict(self._body)


class FakeStorage:
    def __init__(self) -> None:
        self.saved_settings: dict = {}
        self.logs: list[tuple] = []

    async def stats(self):
        return {"memes": 3, "videos": 1, "reviews": 0, "slangs": 2, "transcripts": 1, "fts": True}

    async def list_tasks(self, limit=5, status=None, kind=None):
        return [{"id": "t1", "kind": "video", "status": "done", "progress": 100, "payload": {}}]

    async def find_tasks_by_target(self, target, kind=None):
        return []

    async def list_memes(self, query="", status=None, limit=20, offset=0, source=""):
        return [{"id": 1, "term": "典", "definition": "经典", "status": "published"}]

    async def count_memes(self, status=None, source=""):
        return 1

    async def set_settings(self, changes):
        self.saved_settings.update(changes)

    async def upsert_slang(self, scope, term, definition, aliases, created_by=""):
        return 1

    async def add_log(self, level, action, message):
        self.logs.append((level, action, message))


class FakeProbe:
    def to_dict(self):
        return {"mode": "inprocess", "pending": 2}


class FakeMemewiki:
    def probe(self):
        return FakeProbe()

    async def status(self):
        return {"mode": "inprocess", "pending": 2}


class FakeAuth:
    def __init__(self) -> None:
        self.logged_in = True

    async def status(self, verify=False):
        return {"logged_in": True, "status": "ok", "mid": 1, "uname": "u"}

    async def save_cookie_text(self, raw):
        return {"status": "ok"}

    async def start_qr(self):
        return {"url": "u", "qrcode_key": "k"}

    async def poll_qr(self, key):
        return {"status": "waiting"}


class FakeLLM:
    def list_providers(self):
        return [{"id": "p1", "model": "m1", "type": "openai"}]


class FakeQueue:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    async def submit(self, kind, target, payload):
        self.submitted.append((kind, target, payload))
        return "task-1"

    def cancel(self, task_id):
        return True


class FakeTemp:
    def stats(self):
        return {"files": 0, "bytes": 0, "path": "", "oldest": ""}

    def cleanup(self, days=3):
        return 0


class FakePlugin:
    def __init__(self) -> None:
        self.storage = FakeStorage()
        self.auth = FakeAuth()
        self.memewiki = FakeMemewiki()
        self.llm = FakeLLM()
        self.queue = FakeQueue()
        self.temp = FakeTemp()
        self.version = "0.1.0"
        self._settings = Settings()
        self.started = 0

    async def ensure_started(self):
        self.started += 1

    def settings(self):
        return self._settings

    async def reload_settings(self):
        return self._settings


def run(coro):
    return asyncio.run(coro)


def body(response) -> dict:
    raw = response.body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


@unittest.skipUnless(HAS_ASTRBOT, "需要 AstrBot 运行环境")
class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = FakePlugin()
        self.api = WebApi(self.plugin)

    def use(self, query=None, payload=None) -> None:
        api_module.request = FakeRequest(query=query, body=payload)

    def test_status_combines_sections(self) -> None:
        self.use()
        data = body(run(self.api.status()))["data"]
        self.assertEqual(data["stats"]["memes"], 3)
        self.assertEqual(data["memewiki"]["mode"], "inprocess")
        self.assertEqual(data["providers"][0]["id"], "p1")
        self.assertEqual(data["version"], "0.1.0")
        self.assertEqual(self.plugin.started, 1)

    def test_memes_pagination(self) -> None:
        self.use(query={"query": "典", "limit": "10", "offset": "0"})
        data = body(run(self.api.memes()))["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["term"], "典")

    def test_parse_submits_task(self) -> None:
        self.use(payload={"url": "BV1xx411c7mD"})
        data = body(run(self.api.parse()))["data"]
        self.assertEqual(data["task_id"], "task-1")
        self.assertEqual(self.plugin.queue.submitted[0][0], "video")

    def test_parse_rejects_collection_link(self) -> None:
        self.use(payload={"url": "https://space.bilibili.com/1/channel/collectiondetail?sid=2"})
        payload = body(run(self.api.parse()))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "BAD_URL")

    def test_parse_rejects_empty(self) -> None:
        self.use(payload={})
        payload = body(run(self.api.parse()))
        self.assertFalse(payload["ok"])

    def test_collection_requires_collection_link(self) -> None:
        self.use(payload={"url": "https://www.bilibili.com/video/BV1xx411c7mD"})
        payload = body(run(self.api.collection()))
        self.assertFalse(payload["ok"])

    def test_collection_submits(self) -> None:
        self.use(payload={"url": "https://space.bilibili.com/1/channel/collectiondetail?sid=2"})
        data = body(run(self.api.collection()))["data"]
        self.assertEqual(data["kind"], "collection")
        self.assertEqual(str(self.plugin.queue.submitted[0][2]["season_id"]), "2")

    def test_task_not_found(self) -> None:
        self.use(query={})
        payload = body(run(self.api.task()))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "BAD_REQUEST")

    def test_settings_save_filters_and_persists(self) -> None:
        self.use(payload={"review_required": False, "unknown": 1, "request_interval": "2.5"})
        body(run(self.api.settings_save()))
        self.assertIs(self.plugin.storage.saved_settings["review_required"], False)
        self.assertEqual(self.plugin.storage.saved_settings["request_interval"], 2.5)
        self.assertNotIn("unknown", self.plugin.storage.saved_settings)

    def test_settings_save_empty_rejected(self) -> None:
        self.use(payload={"unknown": 1})
        payload = body(run(self.api.settings_save()))
        self.assertFalse(payload["ok"])

    def test_slang_validation(self) -> None:
        self.use(payload={"scope": "group:1", "term": "x", "definition": "y"})
        payload = body(run(self.api.slang_save()))
        self.assertFalse(payload["ok"])

    def test_slang_save_ok(self) -> None:
        self.use(payload={"scope": "group:1", "term": "绝绝子", "definition": "表示很绝"})
        payload = body(run(self.api.slang_save()))
        self.assertTrue(payload["ok"])

    def test_auth_cookie_requires_value(self) -> None:
        self.use(payload={})
        payload = body(run(self.api.auth_cookie()))
        self.assertFalse(payload["ok"])

    def test_review_resolve_validation(self) -> None:
        self.use(payload={"id": 1, "action": "maybe"})
        payload = body(run(self.api.review_resolve()))
        self.assertFalse(payload["ok"])

    def test_export_returns_file_response(self) -> None:
        async def fake_export(limit=1000):
            from pathlib import Path
            import tempfile

            tmp = Path(tempfile.gettempdir()) / "mh-export.jsonl"
            tmp.write_text('{"name":"x"}\n', encoding="utf-8")
            return tmp

        self.plugin.memewiki.export_jsonl = fake_export  # type: ignore[attr-defined]
        self.use()
        response = run(self.api.memewiki_export())
        self.assertIn("mh-export.jsonl", str(response.headers.get("content-disposition", "")))


if __name__ == "__main__":
    unittest.main()
