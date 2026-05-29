from __future__ import annotations

import json
import http.client
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from echo_repro.config import LLMSettings, get_llm_settings
from echo_repro.llm.base import BaseLLMClient
from echo_repro.models import LLMCallMetadata


# 定义一个 transport 类型：
# 输入 url、headers、payload、timeout
# 返回模型 API 的 JSON 响应
Transport = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]


def _messages_url(base_url: str) -> str:
    """
    根据用户配置的 base_url 拼接 Anthropic messages API 地址。

    如果 base_url 已经以 /v1 结尾，则直接拼接 /messages；
    否则自动补上 /v1/messages。
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def _default_transport(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: int
) -> dict[str, Any]:
    """
    默认 HTTP 请求实现。

    负责：
    1. 将 payload 转成 JSON；
    2. 发送 POST 请求；
    3. 对 429 / 5xx 等临时错误进行最多 3 次重试；
    4. 返回解析后的 JSON 响应。
    """
    body = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None

    # 最多重试 3 次
    for attempt in range(1, 4):
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            # 发送请求并读取响应
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            # HTTPError 说明服务器返回了非 2xx 状态码
            response_body = exc.read().decode("utf-8", errors="replace")

            # 只有限流或服务器错误才重试；
            # 其他错误直接抛出，例如 400 / 401 / 403
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise ValueError(
                    f"Anthropic-compatible request failed with HTTP {exc.code}: {response_body}"
                ) from exc

            last_error = exc

        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ) as exc:
            # 网络错误、超时、连接中断等情况也允许重试
            if attempt == 3:
                raise ValueError(
                    f"Anthropic-compatible request failed after retries: {exc}"
                ) from exc

            last_error = exc

        # 简单退避：第 1 次失败等 1 秒，第 2 次失败等 2 秒
        time.sleep(attempt)

    # 理论上不会走到这里，兜底用
    raise ValueError(f"Anthropic-compatible request failed after retries: {last_error}")


class AnthropicCompatibleLLMClient(BaseLLMClient):
    """
    Anthropic-compatible LLM 客户端。

    作用：
    - 封装 Anthropic Messages API 调用；
    - 支持普通文本生成；
    - 支持 JSON 生成；
    - 记录每次 LLM 调用的 token、延迟等 metadata。
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        transport: Transport | None = None
    ) -> None:
        """
        初始化客户端。

        settings:
            LLM 配置，例如模型名、API key、temperature、timeout 等。

        transport:
            请求发送函数。默认使用 _default_transport。
            这里允许注入 transport，方便单元测试时 mock API 请求。
        """
        self.settings = settings or get_llm_settings()
        self.model_name = self.settings.anthropic_model
        self.temperature = self.settings.temperature

        # 使用 Anthropic 模型时必须提供 API key
        if not self.settings.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY is required when using --llm anthropic."
            )

        self.transport = transport or _default_transport

    def generate_text(self, prompt: str) -> str:
        """
        生成普通文本。

        system prompt 要求模型作为精准的软件工程助手。
        """
        content = self._generate_nonempty_text(
            prompt,
            system="You are a precise software engineering assistant.",
        )
        return content.strip()

    def generate_json(self, prompt: str) -> dict:
        """
        生成 JSON 对象。

        system prompt 强制要求模型只返回合法 JSON，
        不要返回 markdown 代码块。
        """
        content = self._generate_nonempty_text(
            prompt,
            system="You return only valid JSON objects with no markdown fences.",
        )

        # 将模型返回的字符串解析成 Python dict
        return json.loads(content.strip())

    def _generate_nonempty_text(
        self,
        prompt: str,
        system: str,
        attempts: int = 3
    ) -> str:
        """
        调用模型生成非空文本。

        如果模型返回空内容，则最多重试 attempts 次。
        这样可以避免偶发的 empty response 影响主流程。
        """
        last_response: dict[str, Any] | None = None

        for attempt in range(1, attempts + 1):
            response = self._create_message(prompt, system=system)
            content = self._extract_text(response)

            # 只要返回内容不是空字符串，就直接返回
            if content.strip():
                return content

            last_response = response

            # 非最后一次失败时，等待 1 秒再重试
            if attempt < attempts:
                time.sleep(1)

        # 多次尝试后仍然为空，则抛出错误
        stop_reason = last_response.get("stop_reason", "") if last_response else ""
        raise ValueError(
            "Anthropic-compatible model returned empty text content"
            + (
                f" after {attempts} attempts; stop_reason={stop_reason!r}."
                if stop_reason
                else "."
            )
        )

    def _create_message(self, prompt: str, system: str) -> dict[str, Any]:
        """
        构造 Anthropic Messages API 请求并发送。

        同时记录本次调用的 metadata，
        包括模型、latency、token usage 等。
        """
        started = time.perf_counter()

        # Anthropic Messages API 的请求体
        payload = {
            "model": self.settings.anthropic_model,
            "max_tokens": self.settings.max_tokens,
            "temperature": self.settings.temperature,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        }

        # Anthropic API 请求头
        headers = {
            "content-type": "application/json",
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }

        # 发送请求
        response = self.transport(
            _messages_url(self.settings.anthropic_base_url),
            headers,
            payload,
            self.settings.timeout_seconds,
        )

        # 保存最后一次 LLM 调用的元数据
        self.last_call_metadata = self._build_metadata(response, started)

        return response

    def _extract_text(self, response: dict[str, Any]) -> str:
        """
        从 Anthropic Messages API 响应中提取文本内容。

        Anthropic 返回的 content 通常是一个 list，
        每个元素可能是：
        - {"type": "text", "text": "..."}
        - 或其他类型内容

        这里只提取 type == "text" 的部分。
        """
        parts = response.get("content") or []
        texts = []

        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                texts.append(str(part.get("text") or ""))

        # 多个文本块用换行拼接
        return "\n".join(texts)

    def _build_metadata(
        self,
        response: dict[str, Any],
        started: float
    ) -> LLMCallMetadata:
        """
        根据 API 响应构建 LLM 调用元数据。

        主要记录：
        - provider
        - model
        - latency_ms
        - input_tokens
        - output_tokens
        - total_tokens
        - raw_usage
        """
        usage = response.get("usage") or {}

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")

        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = int(input_tokens) + int(output_tokens)

        return LLMCallMetadata(
            provider="anthropic",
            model=self.settings.anthropic_model,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            raw_usage=usage,
        )