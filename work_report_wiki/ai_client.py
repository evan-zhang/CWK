# -*- coding: utf-8 -*-
"""LLM 客户端：newapi（OpenAI 兼容网关）。

- 通过 OpenAI 兼容 /v1/chat/completions 调用
- Bearer 鉴权（API Key 来自 settings.ai_user_key）
- 支持 JSON 模式（response_format=json_object），供 claim 抽取/答案解析使用
- 业务失败后按 RETRY_COUNT 退避重试

newapi 文档：https://docs.newapi.pro/zh
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import requests

from .config import settings

logger = logging.getLogger(__name__)

RETRY_COUNT = 3
API_TIMEOUT = 300
RATE_LIMIT_BACKOFF = 8  # 429 基础退避秒数（指数增长）


class AIClient:
    """newapi OpenAI 兼容聊天客户端。"""

    @staticmethod
    def _chat_url() -> str:
        """由配置的 base url 推导出 OpenAI 兼容的 chat/completions 端点。"""
        base = (settings.ai_url or "").rstrip("/")
        if not base:
            raise ValueError("AI_URL is required for newapi LLM endpoint")
        if not base.endswith("/v1"):
            base = base + "/v1"
        return base + "/chat/completions"

    @staticmethod
    def _get_headers() -> Dict[str, str]:
        if not settings.ai_user_key:
            raise ValueError("AI_USER_KEY is required for newapi LLM endpoint")
        return {
            "Authorization": f"Bearer {settings.ai_user_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def make_request(payload: Dict[str, Any], retry_count: Optional[int] = None) -> Dict[str, Any]:
        """发送一次 OpenAI 兼容请求，返回完整响应体；内部负责重试。"""
        if retry_count is None:
            retry_count = RETRY_COUNT

        url = AIClient._chat_url()
        last_exception: Optional[BaseException] = None
        for attempt in range(retry_count):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers=AIClient._get_headers(),
                    timeout=API_TIMEOUT,
                )
            except requests.exceptions.RequestException as e:  # 网络层异常
                last_exception = e
                logger.error("AI request failed (attempt %s/%s): %s", attempt + 1, retry_count, str(e))
                if attempt < retry_count - 1:
                    time.sleep(min(RATE_LIMIT_BACKOFF * (attempt + 1), 60))
                    continue
                break

            # 429 限流：尊重 Retry-After，否则指数退避后重试（不计入业务错误）
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF * (attempt + 1)
                except (TypeError, ValueError):
                    delay = RATE_LIMIT_BACKOFF * (attempt + 1)
                delay = min(delay, 60)
                logger.warning(
                    "AI 429 限流 (attempt %s/%s)，退避 %.1fs 后重试", attempt + 1, retry_count, delay,
                )
                last_exception = RuntimeError("AI 429 rate limited")
                if attempt < retry_count - 1:
                    time.sleep(delay)
                    continue
                break

            try:
                response.raise_for_status()
            except Exception as e:  # 其它 HTTP 异常
                last_exception = e
                logger.error("AI request failed (attempt %s/%s): %s", attempt + 1, retry_count, str(e))
                if attempt < retry_count - 1:
                    time.sleep(min(RATE_LIMIT_BACKOFF * (attempt + 1), 60))
                    continue
                break

            try:
                body: Any = response.json()
            except ValueError:
                logger.error("AI response is not valid JSON: %s", str(response.text)[:2000])
                raise

            # OpenAI 兼容错误：即便 HTTP 200 也可能在 error 字段返回错误
            if isinstance(body, dict) and body.get("error"):
                err = body["error"]
                err_msg = err.get("message") if isinstance(err, dict) else str(err)
                logger.error(
                    "AI biz error (attempt %s/%s): %s", attempt + 1, retry_count, err_msg,
                )
                last_exception = RuntimeError(f"AI biz error: {err_msg}")
                if attempt < retry_count - 1:
                    continue
                raise last_exception

            return body

        assert last_exception is not None
        raise last_exception

    @staticmethod
    def _extract_content(body: Dict[str, Any]) -> str:
        if not isinstance(body, dict):
            raise RuntimeError(f"Invalid AI response: {body}")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(f"Invalid AI choices: {body}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Invalid AI content: {body}")
        return content.strip()

    @staticmethod
    def chat(
        system_content: str,
        user_content: str,
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        """发起一次聊天补全，返回模型文本。json_mode=True 时要求返回 JSON。

        max_tokens 可覆盖全局 settings.ai_max_output_tokens（如 Wiki 页面编译
        不需要那么长输出，传入更小值以加速并降低限流概率）。
        """
        payload: Dict[str, Any] = {
            "model": settings.ai_model_type,
            "messages": [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            "temperature": settings.ai_temperature,
            "top_p": settings.ai_top_p,
            "max_tokens": max_tokens if max_tokens is not None else settings.ai_max_output_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        body = AIClient.make_request(payload)
        return AIClient._extract_content(body)

    @staticmethod
    def _repair_json(text: str) -> str:
        """修复模型在 JSON 字符串值中写入的未转义换行/回车。

        部分模型（如 MiniMax-M3）在 json 字符串里直接放真实换行，导致
        JSON 非法；这里在不破坏结构的前提下把字符串内的裸换行转义为 \\n。
        """
        out: list = []
        in_str = False
        escaped = False
        for ch in text:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_str = not in_str
                out.append(ch)
                continue
            if in_str and ch == "\n":
                out.append("\\n")
                continue
            if in_str and ch == "\r":
                out.append("\\r")
                continue
            out.append(ch)
        return "".join(out)

    @staticmethod
    def _parse_json(raw: Any) -> Any:
        """鲁棒解析模型返回的 JSON。

        部分模型（如 MiniMax-M3）即便在 json_object 模式下仍会把结果包在
        ```json ... ``` 代码围栏中，或对长输出截断；这里去掉围栏并截取首个
        '{' 到最后一个 '}' 之间的内容，再修复字符串内裸换行后交给 json.loads。
        """
        if not isinstance(raw, str):
            raise RuntimeError(f"Invalid AI content (not str): {raw!r}")
        text = raw.strip()
        # 去掉可能的 ```json ... ``` / ``` ... ``` 代码围栏
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # 退化保护：截取首个 '{' 到最后一个 '}'
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
        if not text:
            raise RuntimeError(f"Empty AI content for JSON parse: {raw!r}")
        text = AIClient._repair_json(text)
        return json.loads(text)

    @staticmethod
    def chat_json(system_content: str, user_content: str, max_tokens: Optional[int] = None) -> Any:
        """发起一次聊天补全并解析为 JSON（供结构化抽取/解析使用）。

        优先用 json_object 模式；若返回内容无法解析（模型仍带代码围栏、
        或 json 模式不被支持），自动去掉 response_format 重试一次。
        max_tokens 可覆盖全局输出上限（见 chat）。
        """
        try:
            raw = AIClient.chat(system_content, user_content, json_mode=True, max_tokens=max_tokens)
            return AIClient._parse_json(raw)
        except (json.JSONDecodeError, RuntimeError):
            logger.warning("chat_json json 模式解析失败，去掉 response_format 重试")
            raw = AIClient.chat(system_content, user_content, json_mode=False, max_tokens=max_tokens)
            return AIClient._parse_json(raw)
