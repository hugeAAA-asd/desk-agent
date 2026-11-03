"""Agent 侧的身份隔离测试。

这一组是第 2 天的验收标准，也是整个项目存在的理由：

- 模型的工具 schema 里没有身份参数；
- 模型就算硬塞 customer_id，也被执行器拒掉；
- 模型拿着别人的单号，查出来是「订单不存在」；
- token 从头到尾没进过模型上下文。

模型用假的。越权能不能拦住是确定性逻辑，掺进真模型的随机性只会让
失败原因变得说不清：是权限没拦住，还是模型今天不想调工具？
"""

import json

import pytest

from agent import loop
from agent.biz_client import BizClient
from agent.loop import run_turn
from agent.session import Session
from agent.tools import TOOLS_SCHEMA
from biz import tokens
from tests.conftest import A_ORDER, B_ORDER, FakeLLM, tool_call

A_TOKEN = tokens.issue("C1001")


@pytest.fixture()
def client(transport):
    """一个带 A 身份的客户端；走真实 httpx + 真实鉴权。"""
    return BizClient("http://biz", A_TOKEN, transport=transport)


@pytest.fixture()
def session():
    return Session(id="test-session", token=A_TOKEN)


def run(monkeypatch, session, client, script):
    fake = FakeLLM(script)
    monkeypatch.setattr(loop, "call_llm", fake)
    return run_turn(session, "查一下我的订单", client), fake


# ── schema 本身就不该有身份维度 ──────────────────────────────────────


def test_schema_has_no_identity_parameter() -> None:
    """工具说明里出现 customer_id，模型就会试着传。

    这条测试不是形式主义：schema 是模型唯一能看见的「能力清单」，
    它的形状决定了模型会做什么。
    """
    dumped = json.dumps(TOOLS_SCHEMA, ensure_ascii=False)
    for word in ("customer_id", "user_id", "客户号", "客户ID"):
        assert word not in dumped


def test_schema_argument_names_match_executor() -> None:
    """schema 声明的参数必须和执行器的白名单一致。

    两处各写一遍迟早会漂移：schema 多一个参数，模型就会传，执行器却拒，
    表现成「模型总是调用失败」，排查方向全错。
    """
    from agent.tools import ALLOWED_ARGS

    for item in TOOLS_SCHEMA:
        fn = item["function"]
        declared = set(fn["parameters"].get("properties", {}))
        assert declared == ALLOWED_ARGS[fn["name"]], fn["name"]


# ── 模型硬塞身份参数 ────────────────────────────────────────────────


def test_model_passing_customer_id_is_rejected(monkeypatch, session, client) -> None:
    """模型如果自作主张加 customer_id，必须被明确拒绝并告知原因。

    不能「静默忽略」——那样模型会以为调用成功了，下一轮接着传，
    而人看到的是一个不知道为什么查不到东西的助手。
    """
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("get_order", f'{{"order_no":"{A_ORDER}","customer_id":"C1002"}}'),
            {"role": "assistant", "content": "查到了"},
        ],
    )
    step = turn.steps[0]
    assert "不接受参数" in step.result
    assert "customer_id" in step.result
    assert "身份由系统" in step.result


def test_identity_rejection_does_not_echo_the_value(monkeypatch, session, client) -> None:
    """拒绝时要报参数名，但不要把模型传的值再回显一遍。

    回显等于把「它刚才试图查谁」写回上下文，模型下一轮可能拿去编故事。
    """
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("get_order", '{"order_no":"SF20261028001","customer_id":"C1002"}'),
            {"role": "assistant", "content": "好的"},
        ],
    )
    assert "C1002" not in turn.steps[0].result


# ── 拿着别人的单号 ──────────────────────────────────────────────────


def test_model_cannot_read_other_customer_order(monkeypatch, session, client) -> None:
    """模型知道 B 的单号也没用：查询在业务系统那一层就被 404 掉了。"""
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("get_order", f'{{"order_no":"{B_ORDER}"}}'),
            {"role": "assistant", "content": "这个订单查不到。"},
        ],
    )
    assert turn.steps[0].result == "订单不存在。当前用户名下没有这个单号，不要猜测它属于别人。"


def test_model_can_read_own_order(monkeypatch, session, client) -> None:
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("get_order", f'{{"order_no":"{A_ORDER}"}}'),
            {"role": "assistant", "content": "已发货"},
        ],
    )
    assert "机械键盘" in turn.steps[0].result
    assert "已发货" in turn.steps[0].result


def test_list_orders_never_contains_others(monkeypatch, session, client) -> None:
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [tool_call("list_my_orders", "{}"), {"role": "assistant", "content": "共 4 单"}],
    )
    result = turn.steps[0].result
    assert "共 4 单" in result
    assert B_ORDER not in result
    assert "人体工学椅" not in result


def test_ticket_on_other_order_is_rejected(monkeypatch, session, client) -> None:
    """写入侧同样拦住：不能把工单挂到别人的单上。"""
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("create_ticket", f'{{"reason":"这单有问题","order_no":"{B_ORDER}"}}'),
            {"role": "assistant", "content": "没能建单"},
        ],
    )
    assert turn.steps[0].result.startswith("订单不存在")


# ── token 不进模型 ──────────────────────────────────────────────────


def test_token_never_enters_messages(monkeypatch, session, client) -> None:
    """跑完一整轮，会话里不该出现 token 的任何一段。

    token 一旦进了 messages，它就会随每次请求发给模型供应商，
    也会出现在日志、报错、前端调试面板里。身份不需要模型知道。
    """
    run(
        monkeypatch,
        session,
        client,
        [tool_call("list_my_orders", "{}"), {"role": "assistant", "content": "共 4 单"}],
    )
    dumped = json.dumps(session.messages, ensure_ascii=False)
    assert A_TOKEN not in dumped
    assert A_TOKEN.split(".")[1] not in dumped


def test_model_never_sees_token_in_any_round(monkeypatch, session, client) -> None:
    """逐轮检查：不管第几轮，发给模型的上下文里都不能有 token。"""
    _, fake = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("list_my_orders", "{}", "c1"),
            tool_call("get_order", f'{{"order_no":"{A_ORDER}"}}', "c2"),
            {"role": "assistant", "content": "查好了"},
        ],
    )
    assert len(fake.calls) == 3
    for call in fake.calls:
        dumped = json.dumps(call["messages"], ensure_ascii=False)
        assert A_TOKEN not in dumped


# ── 坏输入不该把循环打崩 ────────────────────────────────────────────


def test_invalid_json_arguments_are_reported(monkeypatch, session, client) -> None:
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [
            tool_call("get_order", "{not json"),
            {"role": "assistant", "content": "我重新查一下"},
        ],
    )
    assert "不是合法 JSON" in turn.steps[0].result


def test_unknown_tool_is_reported(monkeypatch, session, client) -> None:
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [tool_call("drop_table", "{}"), {"role": "assistant", "content": "没有这个工具"}],
    )
    assert "没有名为 drop_table 的工具" in turn.steps[0].result


def test_empty_arguments_object_is_fine(monkeypatch, session, client) -> None:
    turn, _ = run(
        monkeypatch,
        session,
        client,
        [tool_call("list_my_orders", ""), {"role": "assistant", "content": "共 4 单"}],
    )
    assert "共 4 单" in turn.steps[0].result


# ── 轮次用尽要收口，不能崩 ──────────────────────────────────────────


def test_max_steps_finalizes_instead_of_crashing(monkeypatch, session, client) -> None:
    """模型一直调工具时，最后一轮必须不带工具收尾。

    直接截断会留下「assistant 说要调工具、却没有 tool 回复」，
    下一次请求就是 400——这个坑在 agent-lab 里踩过。
    """
    from agent.config import MAX_STEPS

    script = [tool_call("list_my_orders", "{}", f"c{i}") for i in range(MAX_STEPS + 3)]
    turn, _ = run(monkeypatch, session, client, script)

    assert turn.truncated is True
    assert turn.reply
    assert len(turn.steps) == MAX_STEPS


def test_finalize_call_has_no_tools(monkeypatch, session, client) -> None:
    """收尾那次请求必须不带 tools，否则模型可能继续要工具。"""
    from agent.config import MAX_STEPS

    script = [tool_call("list_my_orders", "{}", f"c{i}") for i in range(MAX_STEPS + 2)]
    _, fake = run(monkeypatch, session, client, script)
    assert fake.calls[-1]["tools"] is None
