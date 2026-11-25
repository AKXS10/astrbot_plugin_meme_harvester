"""后台任务队列：串行 / 限并发执行解析任务，进度落库供 WebUI 轮询。"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Any, Awaitable, Callable

from .config import _as_bool
from .bili.client import BiliError
from .bili.urls import ParsedTarget
from .constants import ErrorCode
from .pipeline import VideoPipeline
from .storage import Storage

SyncFn = Callable[[], Awaitable[Any]]


class _LimiterSlot:
    """``DynamicLimiter.slot(limit)`` 的异步上下文管理器。"""

    def __init__(self, limiter: "DynamicLimiter", limit: int) -> None:
        self._limiter = limiter
        self._limit = limit

    async def __aenter__(self) -> None:
        await self._limiter._acquire(self._limit)

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._limiter._release()


class DynamicLimiter:
    """动态并发上限。

    旧实现每次 ``queue_concurrency`` 变化就换一个新的 ``asyncio.Semaphore``，
    已在跑的任务仍持有旧信号量，新任务立刻拿到新信号量的名额 —— 瞬间超发。
    这里用一个计数 + 条件变量，上限只影响「下一次放行」，不会让在跑任务脱管。
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._active = 0

    def slot(self, limit: int) -> _LimiterSlot:
        return _LimiterSlot(self, max(1, int(limit)))

    async def _acquire(self, limit: int) -> None:
        async with self._cond:
            while self._active >= limit:
                await self._cond.wait()
            self._active += 1

    async def _release(self) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()


class TaskQueue:
    """轻量任务队列。

    AstrBot 是单进程事件循环，解析请求必须异步化：WebAPI 只负责建任务，
    真正的网络/LLM 工作在这里跑，WebUI 通过任务列表轮询进度。
    """

    def __init__(
        self,
        storage: Storage,
        pipeline: VideoPipeline,
        settings_getter: Callable[[], Any],
        *,
        sync_fn: SyncFn | None = None,
        logger: Any = None,
    ) -> None:
        self.storage = storage
        self.pipeline = pipeline
        self._settings = settings_getter
        self._sync_fn = sync_fn
        self._logger = logger
        self._limiter = DynamicLimiter()
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._started = False
        self._submit_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._started:
            return
        failed = await self.storage.fail_stale_tasks()
        if failed and self._logger is not None:
            self._logger.info(f"[meme_harvester] 启动时清理 {failed} 个残留任务")
        self._started = True

    async def shutdown(self) -> None:
        for task in list(self._running.values()):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running.values(), return_exceptions=True)
        self._running.clear()
        self._started = False

    async def submit(self, kind: str, target: str, payload: dict[str, Any] | None = None) -> str:
        """同一链接只保留一条进度：清掉旧记录后重新排队。"""
        payload = payload or {}
        # 加锁串行化：两个并发 submit 同一 target 时，避免同时读到旧记录、
        # 各自 clear/create 导致重复排队或把对方刚创建的任务清掉
        async with self._submit_lock:
            for record in await self.storage.find_tasks_by_target(target, kind=kind):
                await self.clear(record["id"])
            task_id = await self.storage.create_task(kind, target, payload)
        self._spawn(task_id, kind, target, dict(payload))
        return task_id

    async def clear(self, task_id: str) -> bool:
        """取消（若在跑）并删除任务记录。"""
        task = self._running.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            task = self._running.get(task_id)
            if task is not None and not task.done() and not task.cancelled():
                return False
        return await self.storage.delete_task(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self._running.get(task_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def wait(self, task_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
        """测试与命令同步等待用；超时返回当前快照。"""
        task = self._running.get(task_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return await self.storage.get_task(task_id)

    def _spawn(self, task_id: str, kind: str, target: str, payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self._execute(task_id, kind, target, payload))
        self._running[task_id] = task

        def _cleanup(_: asyncio.Task[Any]) -> None:
            self._running.pop(task_id, None)

        task.add_done_callback(_cleanup)

    async def _execute(self, task_id: str, kind: str, target: str, payload: dict[str, Any]) -> None:
        concurrency = max(1, min(4, int(getattr(self._settings(), "queue_concurrency", 1) or 1)))
        async with self._limiter.slot(concurrency):
            await self.storage.update_task(task_id, status="running", progress=1)
            state = {"pct": -100, "ts": 0.0, "stage": ""}
            progress = self._progress_fn(task_id, payload, state)
            try:
                result = await self._dispatch(kind, target, payload, progress)
            except asyncio.CancelledError:
                await self.storage.update_task(task_id, status="cancelled", last_error="用户取消")
                raise
            except BiliError as exc:
                merged = dict(payload)
                merged["stage"] = state["stage"] or merged.get("stage", "")
                merged["result"] = {
                    "status": "failed",
                    "error": exc.code.value,
                    "message": str(exc),
                }
                await self.storage.update_task(
                    task_id,
                    status="failed",
                    progress=100,
                    last_error=exc.code.value,
                    payload=merged,
                )
                await self.storage.add_log(
                    "error", f"task_{kind}", f"{target}: {exc.code.value} {exc}"[:500]
                )
            except Exception as exc:  # noqa: BLE001
                detail = str(exc) or exc.__class__.__name__
                tb = traceback.format_exc()
                if self._logger is not None:
                    self._logger.error("task_%s failed: %s\n%s", kind, detail, tb)
                merged = dict(payload)
                merged["stage"] = state["stage"] or merged.get("stage", "")
                merged["result"] = {
                    "status": "failed",
                    "error": "INTERNAL",
                    "message": detail[:300],
                }
                await self.storage.update_task(
                    task_id,
                    status="failed",
                    progress=100,
                    last_error=f"INTERNAL: {detail}"[:300],
                    payload=merged,
                )
                await self.storage.add_log(
                    "error", f"task_{kind}", f"{target}: {detail}\n{tb}"[:2000]
                )
            else:
                merged = dict(payload)
                merged["result"] = result
                if state["stage"]:
                    merged["stage"] = state["stage"]
                await self.storage.update_task(
                    task_id, status="done", progress=100, payload=merged
                )

    async def _dispatch(
        self,
        kind: str,
        target: str,
        payload: dict[str, Any],
        progress: Callable[[str, int], Awaitable[None]],
    ) -> Any:
        if kind == "video":
            result = await self.pipeline.process_video(
                target,
                page=int(payload.get("page") or 1),
                force=_as_bool(payload.get("force"), False),
                progress=progress,
            )
            return result.to_dict()
        if kind == "url":
            result = await self.pipeline.process_url(
                str(payload.get("url") or target),
                page=int(payload.get("page") or 1),
                force=_as_bool(payload.get("force"), False),
                progress=progress,
            )
            return result.to_dict()
        if kind == "collection":
            parsed = ParsedTarget(
                kind=str(payload.get("target_kind") or "season"),
                raw=target,
                mid=int(payload.get("mid") or 0),
                season_id=int(payload.get("season_id") or 0),
                series_id=int(payload.get("series_id") or 0),
            )
            summary = await self.pipeline.process_collection(
                parsed, force=_as_bool(payload.get("force"), False), progress=progress
            )
            return summary
        if kind == "sync":
            if self._sync_fn is None:
                raise BiliError(ErrorCode.MEMEWIKI_UNAVAILABLE, "同步器未配置")
            sync_result = await self._sync_fn()
            return sync_result.to_dict() if hasattr(sync_result, "to_dict") else sync_result
        raise BiliError(ErrorCode.INTERNAL, f"未知任务类型: {kind}")

    def _progress_fn(
        self, task_id: str, payload: dict[str, Any], state: dict[str, Any]
    ) -> Callable[[str, int], Awaitable[None]]:
        async def report(stage: str, pct: int) -> None:
            now = time.monotonic()
            pct = max(0, min(100, int(pct)))
            if (
                pct >= 100
                or stage != state["stage"]
                or pct - state["pct"] >= 5
                or now - state["ts"] >= 2.0
            ):
                state.update({"pct": pct, "ts": now, "stage": stage})
                merged = dict(payload)
                merged["stage"] = stage
                await self.storage.update_task(task_id, progress=pct, payload=merged)

        return report
