"""模型调用：整个仓库只有这一处知道 API key 长什么样。"""

import json
import time
from collections.abc import Iterator
from typing import Any

import httpx

from .config import LLM_API_KEY, LLM_MODEL, LLM_URL

RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """模型调用失败。上层决定是重试、降级还是直接告诉用户。"""


def _payload(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream"] = True
    return payload


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LLM_API_KEY}"}


def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    retries: int = 3,
) -> dict[str, Any]:
    """返回 assistant message（含可能的 tool_calls）。

    注意返回的是**整条 message**，不是只取 content：DeepSeek 的推理模型会在
    里面带 `reasoning_content`，下一轮必须原样回传，否则会 400。
    这一点在 agent-lab 里踩过，这里直接按「整条存取」写，不做字段裁剪。
    """
    if not LLM_API_KEY:
        raise LLMError("没有配置 DEEPSEEK_API_KEY，无法调用模型")

    payload = _payload(messages, tools, temperature, stream=False)

    last_error = ""
    for attempt in range(retries):
        try:
            resp = httpx.post(LLM_URL, headers=_headers(), json=payload, timeout=60.0)
        except httpx.HTTPError as exc:
            last_error = f"网络错误：{exc.__class__.__name__}"
        else:
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]
            last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
            if resp.status_code not in RETRY_STATUS:
                break

        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))

    raise LLMError(last_error)


def stream_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
) -> Iterator[tuple[str, Any]]:
    """流式调用。产出 `("delta", 文本片段)`，最后产出 `("message", 完整消息)`。

    为什么要拼装完整消息：流式响应里的 `tool_calls` 是**增量**的，
    名字和参数会拆成好几段（`{"order_no":` 一段、`"SF2026..."}` 另一段）。
    不拼装就解析不出参数，工具调用会全废。

    不做重试：流已经吐了一半再重来，前端会看到重复的字。
    失败就让上层把错误当成一次事件推下去，用户至少知道发生了什么。
    """
    if not LLM_API_KEY:
        raise LLMError("没有配置 DEEPSEEK_API_KEY，无法调用模型")

    payload = _payload(messages, tools, temperature, stream=True)
    content_parts: list[str] = []
    calls: dict[int, dict[str, Any]] = {}

    with httpx.stream("POST", LLM_URL, headers=_headers(), json=payload, timeout=120.0) as resp:
        if resp.status_code != 200:
            body = resp.read().decode("utf-8", "ignore")
            raise LLMError(f"HTTP {resp.status_code}：{body[:200]}")

        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue

            choices = chunk.get("choices") or [{}]
            delta = choices[0].get("delta") or {}

            text = delta.get("content")
            if text:
                content_parts.append(text)
                yield ("delta", text)

            for part in delta.get("tool_calls") or []:
                index = int(part.get("index", 0))
                slot = calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if part.get("id"):
                    slot["id"] = part["id"]
                function = part.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    yield ("message", message)
