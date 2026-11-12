"""LLM 接入：包装 AstrBot 的 llm_generate，处理 JSON 模式降级与超时。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from .constants import ERROR_MESSAGES, ErrorCode


class LLMError(Exception):
    def __init__(self, code: ErrorCode, message: str = "") -> None:
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, code.value)
        super().__init__(self.message)


class LLMService:
    """把 AstrBot Provider 包装成「给提示词、拿文本」的简单函数。"""

    def __init__(self, context: Any, settings_getter: Callable[[], Any]) -> None:
        self._context = context
        self._settings_getter = settings_getter
        self._json_mode_ok = True

    # ---------------------------------------------------------------- 信息

    def list_providers(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        try:
            providers = self._context.get_all_providers() or []
        except Exception:
            return result
        for provider in providers:
            try:
                meta = provider.meta()
            except Exception:
                continue
            result.append(
                {
                    "id": str(getattr(meta, "id", "") or ""),
                    "model": str(getattr(meta, "model", "") or ""),
                    "type": str(getattr(meta, "type", "") or ""),
                }
            )
        return result

    async def _resolve_provider_id(self) -> str:
        settings = self._settings_getter()
        configured = (settings.provider_id or "").strip()
        if configured:
            provider = None
            try:
                provider = self._context.get_provider_by_id(configured)
            except Exception:
                provider = None
            if provider is None:
                raise LLMError(
                    ErrorCode.PROVIDER_NOT_CONFIGURED,
                    f"配置的 Provider「{configured}」不存在，请在插件设置中重新选择。",
                )
            return configured
        try:
            provider = await self._context.get_using_provider_async(umo=None)
        except Exception:
            provider = None
        if provider is None:
            raise LLMError(ErrorCode.PROVIDER_NOT_CONFIGURED)
        try:
            meta = provider.meta()
            return str(getattr(meta, "id", "") or "")
        except Exception:
            raise LLMError(ErrorCode.PROVIDER_NOT_CONFIGURED) from None

    # ---------------------------------------------------------------- 调用

    async def complete_json(self, system_prompt: str, user_prompt: str) -> str:
        settings = self._settings_getter()
        provider_id = await self._resolve_provider_id()
        kwargs: dict[str, Any] = {}
        if settings.json_mode and self._json_mode_ok:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            return await asyncio.wait_for(
                self._call(provider_id, system_prompt, user_prompt, kwargs, settings),
                timeout=max(5, int(settings.llm_timeout)),
            )
        except asyncio.TimeoutError:
            raise LLMError(ErrorCode.LLM_FAILED, f"模型调用超时（{settings.llm_timeout}s）。") from None
        except LLMError:
            raise
        except Exception as exc:
            text = str(exc)
            if kwargs and ("response_format" in text or "json_object" in text or "400" in text):
                # Provider 不支持 JSON 模式：关掉它，下一轮起不再尝试
                self._json_mode_ok = False
                try:
                    return await asyncio.wait_for(
                        self._call(provider_id, system_prompt, user_prompt, {}, settings),
                        timeout=max(5, int(settings.llm_timeout)),
                    )
                except Exception as retry_exc:
                    raise LLMError(ErrorCode.LLM_FAILED, str(retry_exc)[:300]) from None
            raise LLMError(ErrorCode.LLM_FAILED, text[:300]) from None

    async def _call(
        self,
        provider_id: str,
        system_prompt: str,
        user_prompt: str,
        kwargs: dict[str, Any],
        settings: Any,
    ) -> str:
        response = await self._context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=user_prompt,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            **kwargs,
        )
        text = getattr(response, "completion_text", "") or ""
        if not text.strip():
            raise LLMError(ErrorCode.LLM_FAILED, "模型返回了空内容。")
        return text


def parse_llm_json(text: str) -> dict[str, Any] | None:
    """容错解析：去代码围栏、截取首个 { 到末个 }。"""
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    brace = cleaned.find("{")
    bracket = cleaned.find("[")
    if bracket >= 0 and (brace < 0 or bracket < brace):
        end = cleaned.rfind("]")
        if end > bracket:
            try:
                payload = json.loads(cleaned[bracket : end + 1])
            except ValueError:
                return None
            return {"memes": payload} if isinstance(payload, list) else None
        return None
    if brace >= 0:
        end = cleaned.rfind("}")
        if end > brace:
            cleaned = cleaned[brace : end + 1]
    try:
        payload = json.loads(cleaned)
    except ValueError:
        return None
    if isinstance(payload, list):
        return {"memes": payload}
    if isinstance(payload, dict):
        return payload
    return None


async def complete_with_retry(
    fn: Callable[[str, str], Awaitable[str]],
    system_prompt: str,
    user_prompt: str,
) -> str:
    """JSON 解析失败时把错误回喂一次（由调用方负责解析）。"""
    return await fn(system_prompt, user_prompt)
