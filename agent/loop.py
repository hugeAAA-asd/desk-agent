"""一轮对话的执行循环。

骨架和 agent-lab 里的手写 ReAct 是同一套，差别只在工具的实现：
那边工具是本地函数，这里工具是 HTTP 调用，而请求头里的身份来自 Session——
模型全程看不到它。

轮次用尽的处理沿用那边的教训：**不能直接把未执行的 tool_calls 丢掉**，
下一次请求会因为「assistant 说要调工具、却没有对应的 tool 回复」而 400。
正确顺序是：先补跑工具 → 再让模型不带工具收尾。
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import audit
from .biz_client import BizClient
from .config import BIZ_BASE_URL, MAX_STEPS
from .llm import LLMError, call_llm, stream_chat
from .session import Session
from .tools import TOOLS_SCHEMA, execute

SYSTEM_PROMPT = """你是电商售后客服助手，服务的是当前已登录的用户。

规则：
1. 只处理订单、物流、退换货、售后相关的问题。其它问题礼貌说明你只负责售后。
2. 涉及订单事实（买了什么、到哪了、多少钱）必须先调工具查，不要凭记忆或推测回答。
3. 政策、规则、时效、费用类问题（退换货条件、运费谁出、保修多久、发票怎么开、价保）
   必须先调 search_policy 查条款，并按原文回答。如果返回「没有找到相关条款」，
   如实说明你不确定，并主动建议转人工，绝不自己编天数、金额或责任划分。
4. 工具说「订单不存在」就是查不到，不要猜它属于别人，也不要反复换写法试探。
5. 用户要求投诉、退换货，或问题需要人工介入时，调用 create_ticket 建单，并把工单号告诉用户。
6. 回答简短、直接，用中文。不要说原文和工具结果里没有的天数、金额或承诺。
"""


@dataclass
class Step:
    """一次工具调用的记录，用于回给前端和排查。"""

    tool: str
    args: dict[str, Any]
    result: str


@dataclass
class Turn:
    reply: str
    steps: list[Step] = field(default_factory=list)
    rounds: int = 0
    truncated: bool = False


def ensure_system(session: Session) -> None:
    if not session.messages or session.messages[0].get("role") != "system":
        session.messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})


def run_turn(session: Session, user_text: str, client: BizClient | None = None) -> Turn:
    """跑一轮：用户说话 → 模型可能调若干次工具 → 给出回复。"""
    ensure_system(session)
    session.messages.append({"role": "user", "content": user_text})

    owns_client = client is None
    client = client or BizClient(BIZ_BASE_URL, session.token)
    steps: list[Step] = []

    try:
        for round_no in range(1, MAX_STEPS + 1):
            message = call_llm(session.messages, tools=TOOLS_SCHEMA)
            session.messages.append(message)

            calls = message.get("tool_calls") or []
            if not calls:
                session.touch()
                return Turn(
                    reply=(message.get("content") or "").strip(),
                    steps=steps,
                    rounds=round_no,
                )

            steps.extend(_run_calls(calls, session, client))

        # 到上限了还在调工具：先把这一批跑完，再让模型不带工具收口
        last = session.messages[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            steps.extend(_run_calls(last["tool_calls"], session, client))

        return _finalize(session, steps, MAX_STEPS)
    finally:
        if owns_client:
            client.close()


def _run_calls(calls: list[dict[str, Any]], session: Session, client: BizClient) -> list[Step]:
    """执行一批工具调用，并把结果按 tool_call_id 配对追加。"""
    done: list[Step] = []
    for call in calls:
        call_id = call.get("id") or ""
        name, args, error = _parse_call(call)
        result = error if error else execute(name, args, client)
        session.messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
        audit.record(
            session_id=session.id,
            customer_id=session.customer_id,
            tool=name,
            args=args,
            result=result,
            ok=_looks_ok(result),
        )
        done.append(Step(tool=name, args=args, result=result))
    return done


def _looks_ok(result: str) -> bool:
    """工具返回的是给模型看的文本，没有结构化状态。

    所有失败分支都以「错误」「调用失败」「订单不存在」开头（见 tools.execute），
    所以前缀判断够用。真要精确，应该让 execute 返回 (text, ok)——但那会让
    每个分支多一行解包，收益不值。
    """
    return not result.startswith(("错误", "调用失败", "订单不存在"))


def _parse_call(call: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """解析模型给的函数调用。解析失败也要回灌，不能让循环崩掉。"""
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    raw = function.get("arguments")

    if raw in (None, ""):
        return name, {}, None
    if isinstance(raw, dict):
        return name, raw, None
    try:
        args = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return name, {}, f"错误：参数不是合法 JSON（{str(raw)[:60]}），请重新调用。"
    if not isinstance(args, dict):
        return name, {}, "错误：参数必须是 JSON 对象。"
    return name, args, None


def _finalize(session: Session, steps: list[Step], rounds: int) -> Turn:
    """不带工具收尾：用已经拿到的信息作答，而不是抛异常或返回空串。"""
    message = call_llm(session.messages, tools=None)
    session.messages.append(message)
    session.touch()
    reply = (message.get("content") or "").strip()
    if not reply:
        reply = "抱歉，这一轮没能查到结果。您可以换个说法，或者让我转人工处理。"
    return Turn(reply=reply, steps=steps, rounds=rounds, truncated=True)


# ── 流式版本 ────────────────────────────────────────────────────────
#
# 和 run_turn 是两份代码而不是一份加开关，是有意的：流式的状态机更碎
# （文本片段、工具阶段、收尾），硬塞进同一个函数会让两边都难读。
# 代价是两份可能漂移——所以两者共用 _run_calls / _parse_call / ensure_system，
# 只在「怎么产出结果」这一层分叉。


def _step_dict(step: Step) -> dict[str, Any]:
    return {"tool": step.tool, "args": step.args, "result": step.result}


def _stream_once(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None):
    """跑一次流式调用，边推 delta 边攒出完整 message。"""
    message: dict[str, Any] | None = None
    for kind, payload in stream_chat(messages, tools=tools):
        if kind == "delta":
            yield ("delta", payload)
        else:
            message = payload
    if message is None:
        raise LLMError("模型没有返回任何内容")
    yield ("message", message)


def run_turn_stream(
    session: Session, user_text: str, client: BizClient | None = None
) -> Iterator[dict[str, Any]]:
    """产出三种事件：`step`（工具调用）、`delta`（文本片段）、`done`（收尾）。"""
    ensure_system(session)
    session.messages.append({"role": "user", "content": user_text})

    owns_client = client is None
    client = client or BizClient(BIZ_BASE_URL, session.token)
    steps: list[Step] = []

    try:
        for round_no in range(1, MAX_STEPS + 1):
            message: dict[str, Any] | None = None
            for kind, payload in _stream_once(session.messages, TOOLS_SCHEMA):
                if kind == "delta":
                    yield {"type": "delta", "text": payload}
                else:
                    message = payload
            if message is None:
                raise LLMError("模型没有返回任何内容")

            session.messages.append(message)
            calls = message.get("tool_calls") or []
            if not calls:
                session.touch()
                yield {
                    "type": "done",
                    "reply": (message.get("content") or "").strip(),
                    "steps": [_step_dict(s) for s in steps],
                    "rounds": round_no,
                    "truncated": False,
                }
                return

            for call in calls:
                for step in _run_calls([call], session, client):
                    steps.append(step)
                    yield {"type": "step", **_step_dict(step)}

        # 轮次用尽：补跑未执行的工具，再不带工具流式收尾
        last = session.messages[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            for step in _run_calls(last["tool_calls"], session, client):
                steps.append(step)
                yield {"type": "step", **_step_dict(step)}

        message = None
        for kind, payload in _stream_once(session.messages, None):
            if kind == "delta":
                yield {"type": "delta", "text": payload}
            else:
                message = payload
        session.messages.append(message or {"role": "assistant", "content": ""})
        session.touch()

        reply = ((message or {}).get("content") or "").strip()
        yield {
            "type": "done",
            "reply": reply or "抱歉，这一轮没能查到结果。您可以换个说法，或者让我转人工处理。",
            "steps": [_step_dict(s) for s in steps],
            "rounds": MAX_STEPS,
            "truncated": True,
        }
    finally:
        if owns_client:
            client.close()
