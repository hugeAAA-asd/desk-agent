"""模型调用：整个仓库只有这一处知道 API key 长什么样。"""

import time
from typing import Any

import httpx

from .config import LLM_API_KEY, LLM_MODEL, LLM_URL

RETRY_STATUS = {429, 500, 502, 503, 504}


class LLMError(Exception):
    """模型调用失败。上层决定是重试、降级还是直接告诉用户。"""


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

    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    last_error = ""
    for attempt in range(retries):
        try:
            resp = httpx.post(
                LLM_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=payload,
                timeout=60.0,
            )
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
