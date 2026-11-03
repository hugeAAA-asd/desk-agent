"""一轮对话的执行循环。

骨架和 agent-lab 里的手写 ReAct 是同一套，差别只在工具的实现：
那边工具是本地函数，这里工具是 HTTP 调用，而请求头里的身份来自 Session——
模型全程看不到它。

轮次用尽的处理沿用那边的教训：**不能直接把未执行的 tool_calls 丢掉**，
下一次请求会因为「assistant 说要调工具、却没有对应的 tool 回复」而 400。
正确顺序是：先补跑工具 → 再让模型不带工具收尾。
"""

import json
from dataclasses import dataclass, field
from typing import Any

from .biz_client import BizClient
from .config import BIZ_BASE_URL, MAX_STEPS
from .llm import call_llm
from .session import Session
from .tools import TOOLS_SCHEMA, execute

SYSTEM_PROMPT = """你是电商售后客服助手，服务的是当前已登录的用户。

规则：
1. 只处理订单、物流、退换货、售后相关的问题。其它问题礼貌说明你只负责售后。
2. 涉及订单事实（买了什么、到哪了、多少钱）必须先调工具查，不要凭记忆或推测回答。
3. 工具说「订单不存在」就是查不到，不要猜它属于别人，也不要反复换写法试探。
4. 用户要求投诉、退换货，或问题需要人工介入时，调用 create_ticket 建单，并把工单号告诉用户。
5. 回答简短、直接，用中文。不要编造物流时间、金额、政策条款。
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
        done.append(Step(tool=name, args=args, result=result))
    return done


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
