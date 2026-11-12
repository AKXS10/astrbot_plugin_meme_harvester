"""插件 WebAPI：全部路由带插件名前缀，供 views 页面通过 bridge 调用。"""

from __future__ import annotations

import functools
from dataclasses import asdict, fields
from typing import Any, Callable

from astrbot.api.web import file_response, json_response, request

from .auth import AuthError
from .bili.client import BiliError
from .bili.urls import parse_target
from .constants import ERROR_MESSAGES, PLUGIN_NAME, ErrorCode

PREFIX = f"/{PLUGIN_NAME}"
_BOOL_KEYS = {
    "json_mode",
    "review_required",
    "memewiki_enabled",
    "auto_parse_on_link",
    "inject_enabled",
}
_INT_KEYS = {
    "max_tokens",
    "llm_timeout",
    "chunk_chars",
    "max_chunks",
    "collection_page_size",
    "max_collection_items",
    "queue_concurrency",
    "raw_retention_days",
    "inject_max_chars",
}
_FLOAT_KEYS = {"temperature", "request_interval", "memewiki_min_confidence"}


def _ok(data: Any = None, **extra: Any) -> Any:
    body: dict[str, Any] = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return json_response(body)


def _fail(code: str, message: str = "", status_code: int = 400) -> Any:
    hint = ""
    try:
        hint = ERROR_MESSAGES.get(ErrorCode(code), "")
    except ValueError:
        hint = ""
    return json_response(
        {"ok": False, "error": code, "message": message or hint or code},
        status_code=status_code,
    )


def _api(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def wrapper(self, *args: Any, **kwargs: Any):
        try:
            starter = getattr(self.plugin, "ensure_started", None)
            if starter is not None:
                await starter()
            return await fn(self, *args, **kwargs)
        except AuthError as exc:
            return _fail(exc.code.value, str(exc), 400)
        except BiliError as exc:
            status = 502 if exc.code in (ErrorCode.RISK_CONTROL, ErrorCode.INTERNAL) else 400
            return _fail(exc.code.value, str(exc), status)
        except ValueError as exc:
            return _fail("BAD_REQUEST", str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            log = getattr(self.plugin, "logger", None)
            if log is not None:
                log.exception("WebAPI 处理失败: %s", exc)
            return _fail("INTERNAL", str(exc), 500)

    return wrapper


async def _body() -> dict[str, Any]:
    data = await request.json(default={})
    return data if isinstance(data, dict) else {}


def _to_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, result))


class WebApi:
    """把插件能力暴露成一组 ``/{plugin_name}/...`` 路由。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    # -------------------------------------------------------------- 注册

    def register(self, context) -> None:
        routes = [
            ("/status", self.status, ["GET"], "插件总览：统计、登录态、模型、梗百科状态"),
            ("/auth/qrcode", self.auth_qrcode, ["POST"], "申请 B 站扫码登录二维码"),
            ("/auth/poll", self.auth_poll, ["GET"], "轮询扫码状态"),
            ("/auth/status", self.auth_status, ["GET"], "查询登录态（verify=1 时校验）"),
            ("/auth/cookie", self.auth_cookie, ["POST"], "粘贴 Cookie 登录"),
            ("/auth/logout", self.auth_logout, ["POST"], "退出登录并清除本地 Cookie"),
            ("/parse", self.parse, ["POST"], "解析单个视频链接"),
            ("/collection", self.collection, ["POST"], "解析 B 站合集 / 系列"),
            ("/tasks", self.tasks, ["GET"], "任务列表"),
            ("/task", self.task, ["GET"], "任务详情"),
            ("/task/cancel", self.task_cancel, ["POST"], "取消任务"),
            ("/memes", self.memes, ["GET"], "词条列表"),
            ("/meme/update", self.meme_update, ["POST"], "更新词条"),
            ("/meme/delete", self.meme_delete, ["POST"], "删除词条"),
            ("/reviews", self.reviews, ["GET"], "审核队列"),
            ("/review/resolve", self.review_resolve, ["POST"], "通过 / 驳回审核项"),
            ("/slangs", self.slangs, ["GET"], "群黑话列表"),
            ("/slang/save", self.slang_save, ["POST"], "新增或更新群黑话"),
            ("/slang/delete", self.slang_delete, ["POST"], "删除群黑话"),
            ("/settings", self.settings, ["GET"], "读取运行设置"),
            ("/settings/save", self.settings_save, ["POST"], "保存运行设置"),
            ("/providers", self.providers, ["GET"], "可用的对话模型 Provider"),
            ("/memewiki/status", self.memewiki_status, ["GET"], "梗百科适配状态"),
            ("/memewiki/sync", self.memewiki_sync, ["POST"], "同步词条到梗百科"),
            ("/memewiki/export", self.memewiki_export, ["GET"], "导出 JSONL 备份"),
            ("/logs", self.logs, ["GET"], "同步/任务日志"),
        ]
        for path, handler, methods, desc in routes:
            context.register_web_api(PREFIX + path, handler, methods, desc)

    # -------------------------------------------------------------- 总览

    @_api
    async def status(self):
        p = self.plugin
        stats = await p.storage.stats()
        probe = p.memewiki.probe()
        auth = await p.auth.status()
        tasks = await p.storage.list_tasks(limit=5)
        return _ok(
            {
                "stats": stats,
                "auth": auth,
                "memewiki": probe.to_dict(),
                "providers": p.llm.list_providers(),
                "settings": asdict(p.settings()),
                "settings_warnings": p.settings().validate(),
                "tasks": tasks,
                "version": p.version,
            }
        )

    # -------------------------------------------------------------- 登录

    @_api
    async def auth_qrcode(self):
        data = await self.plugin.auth.start_qr()
        return _ok(data)

    @_api
    async def auth_poll(self):
        key = str(request.query.get("key", "") or "")
        data = await self.plugin.auth.poll_qr(key)
        return _ok(data)

    @_api
    async def auth_status(self):
        verify = str(request.query.get("verify", "0")) in ("1", "true", "yes")
        return _ok(await self.plugin.auth.status(verify=verify))

    @_api
    async def auth_cookie(self):
        body = await _body()
        raw = str(body.get("cookie") or "").strip()
        if not raw:
            return _fail("BAD_REQUEST", "Cookie 不能为空")
        return _ok(await self.plugin.auth.save_cookie_text(raw))

    @_api
    async def auth_logout(self):
        await self.plugin.auth.logout()
        return _ok({"logged_in": False})

    # -------------------------------------------------------------- 解析

    @_api
    async def parse(self):
        body = await _body()
        raw = str(body.get("url") or "").strip()
        if not raw:
            return _fail("BAD_URL", "请输入视频链接或 BV 号")
        target = parse_target(raw)
        if target.kind == "short":
            final = await self.plugin.client.resolve_short_link(raw)
            target = parse_target(final)
        if target.kind == "unsupported":
            return _fail(ErrorCode.BAD_URL.value, target.reason or "无法识别的链接")
        if target.kind in ("season", "series"):
            return _fail(
                "BAD_URL",
                "这是合集/系列链接，请到「合集解析」页提交（会逐条解析并入库）",
            )
        if target.kind != "video":
            return _fail(ErrorCode.BAD_URL.value, "暂不支持该链接类型")
        force = bool(body.get("force"))
        page = _to_int(body.get("page") or target.page, 1, 1, 9999)
        video_key = target.bvid or f"av{target.aid}"
        task_id = await self.plugin.queue.submit(
            "video", video_key, {"page": page, "force": force}
        )
        return _ok({"task_id": task_id, "target": video_key, "kind": "video"})

    @_api
    async def collection(self):
        body = await _body()
        raw = str(body.get("url") or "").strip()
        if not raw:
            return _fail("BAD_URL", "请输入合集/系列链接")
        target = parse_target(raw)
        if target.kind not in ("season", "series"):
            return _fail(ErrorCode.BAD_URL.value, "不是合集/系列链接")
        if not target.mid:
            return _fail(ErrorCode.BAD_URL.value, "链接里缺少 UP 主 mid")
        payload = {
            "target_kind": target.kind,
            "mid": target.mid,
            "season_id": target.season_id,
            "series_id": target.series_id,
            "force": bool(body.get("force")),
        }
        name = f"season:{target.season_id}" if target.season_id else f"series:{target.series_id}"
        task_id = await self.plugin.queue.submit("collection", name, payload)
        return _ok({"task_id": task_id, "target": name, "kind": "collection"})

    @_api
    async def tasks(self):
        limit = _to_int(request.query.get("limit", 20), 20, 1, 200)
        status = request.query.get("status") or None
        return _ok(await self.plugin.storage.list_tasks(limit=limit, status=status))

    @_api
    async def task(self):
        task_id = str(request.query.get("id", "") or "")
        if not task_id:
            return _fail("BAD_REQUEST", "缺少任务 id")
        record = await self.plugin.storage.get_task(task_id)
        if not record:
            return _fail("NOT_FOUND", "任务不存在", 404)
        return _ok(record)

    @_api
    async def task_cancel(self):
        body = await _body()
        task_id = str(body.get("id") or "")
        cancelled = self.plugin.queue.cancel(task_id)
        if not cancelled:
            await self.plugin.storage.update_task(
                task_id, status="cancelled", last_error="用户取消"
            )
        return _ok({"cancelled": True})

    # -------------------------------------------------------------- 词条

    @_api
    async def memes(self):
        query = str(request.query.get("query", "") or "")
        status = request.query.get("status") or None
        limit = _to_int(request.query.get("limit", 20), 20, 1, 200)
        offset = _to_int(request.query.get("offset", 0), 0, 0, 1_000_000)
        items = await self.plugin.storage.list_memes(
            query=query, status=status, limit=limit, offset=offset
        )
        total = await self.plugin.storage.count_memes(status=status)
        return _ok({"items": items, "total": total, "limit": limit, "offset": offset})

    @_api
    async def meme_update(self):
        body = await _body()
        meme_id = _to_int(body.get("id"), 0, 0, 10**12)
        if not meme_id:
            return _fail("BAD_REQUEST", "缺少词条 id")
        fields_in: dict[str, Any] = {}
        if "definition" in body:
            fields_in["definition"] = str(body.get("definition") or "")[:300]
        if "usage" in body:
            fields_in["usage"] = str(body.get("usage") or "")[:300]
        if "origin" in body:
            fields_in["origin"] = str(body.get("origin") or "")[:500]
        if "tags" in body:
            fields_in["tags"] = body.get("tags") or []
        if "aliases" in body:
            fields_in["aliases"] = body.get("aliases") or []
        if "status" in body:
            status = str(body.get("status") or "")
            if status not in ("draft", "published", "archived"):
                return _fail("BAD_REQUEST", "非法状态")
            fields_in["status"] = status
        ok = await self.plugin.storage.update_meme(meme_id, **fields_in)
        if not ok:
            return _fail("NOT_FOUND", "词条不存在", 404)
        return _ok({"updated": True})

    @_api
    async def meme_delete(self):
        body = await _body()
        meme_id = _to_int(body.get("id"), 0, 0, 10**12)
        if not meme_id:
            return _fail("BAD_REQUEST", "缺少词条 id")
        return _ok({"deleted": await self.plugin.storage.delete_meme(meme_id)})

    @_api
    async def reviews(self):
        limit = _to_int(request.query.get("limit", 50), 50, 1, 200)
        status = str(request.query.get("status", "pending") or "pending")
        return _ok(
            {
                "items": await self.plugin.storage.list_reviews(status=status, limit=limit),
                "pending": await self.plugin.storage.count_reviews(),
            }
        )

    @_api
    async def review_resolve(self):
        body = await _body()
        review_id = _to_int(body.get("id"), 0, 0, 10**12)
        action = str(body.get("action") or "").strip()
        if not review_id or action not in ("approve", "reject"):
            return _fail("BAD_REQUEST", "参数不合法")
        return _ok({"resolved": await self.plugin.storage.resolve_review(review_id, action)})

    # -------------------------------------------------------------- 黑话

    @_api
    async def slangs(self):
        scope = request.query.get("scope") or None
        return _ok(await self.plugin.storage.list_slangs(scope=scope, limit=500))

    @_api
    async def slang_save(self):
        body = await _body()
        scope = str(body.get("scope") or "").strip()
        term = str(body.get("term") or "").strip()
        definition = str(body.get("definition") or "").strip()
        if not scope:
            return _fail("BAD_REQUEST", "请先填写作用域（如 group:123456）")
        if not (2 <= len(term) <= 24):
            return _fail("BAD_REQUEST", "黑话名称需 2–24 字")
        if not definition:
            return _fail("BAD_REQUEST", "释义不能为空")
        aliases = body.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [item.strip() for item in aliases.replace("，", ",").split(",") if item.strip()]
        slang_id = await self.plugin.storage.upsert_slang(
            scope, term, definition[:300], aliases, created_by=str(body.get("by") or "")
        )
        return _ok({"id": slang_id})

    @_api
    async def slang_delete(self):
        body = await _body()
        scope = str(body.get("scope") or "")
        term = str(body.get("term") or "")
        return _ok({"deleted": await self.plugin.storage.delete_slang(scope, term)})

    # -------------------------------------------------------------- 设置

    @_api
    async def settings(self):
        return _ok(asdict(self.plugin.settings()))

    @_api
    async def settings_save(self):
        body = await _body()
        allowed = {f.name for f in fields(self.plugin.settings())}
        changes: dict[str, Any] = {}
        for key, value in body.items():
            if key not in allowed:
                continue
            if key in _BOOL_KEYS:
                changes[key] = bool(value)
            elif key in _INT_KEYS:
                try:
                    changes[key] = int(value)
                except (TypeError, ValueError):
                    continue
            elif key in _FLOAT_KEYS:
                try:
                    changes[key] = float(value)
                except (TypeError, ValueError):
                    continue
            else:
                changes[key] = str(value or "").strip()
        if not changes:
            return _fail("BAD_REQUEST", "没有可保存的设置项")
        await self.plugin.storage.set_settings(changes)
        await self.plugin.reload_settings()
        settings = self.plugin.settings()
        return _ok({"settings": asdict(settings), "warnings": settings.validate()})

    @_api
    async def providers(self):
        return _ok(self.plugin.llm.list_providers())

    # ------------------------------------------------------------ 梗百科

    @_api
    async def memewiki_status(self):
        return _ok(await self.plugin.memewiki.status())

    @_api
    async def memewiki_sync(self):
        body = await _body()
        limit = _to_int(body.get("limit", 200), 200, 1, 1000)
        result = await self.plugin.memewiki.sync(limit=limit, dry_run=bool(body.get("dry_run")))
        data = result.to_dict() if hasattr(result, "to_dict") else result
        await self.plugin.storage.add_log(
            "info", "memewiki_sync", f"同步完成：{data}"
        )
        return _ok(data)

    @_api
    async def memewiki_export(self):
        path = await self.plugin.memewiki.export_jsonl(limit=1000)
        return file_response(
            path, filename=path.name, content_type="application/x-ndjson"
        )

    @_api
    async def logs(self):
        limit = _to_int(request.query.get("limit", 50), 50, 1, 500)
        level = request.query.get("level") or None
        return _ok(await self.plugin.storage.list_logs(limit=limit, level=level))
