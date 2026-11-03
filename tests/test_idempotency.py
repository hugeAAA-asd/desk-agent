"""幂等与审计。

幂等要证明的是「重复提交只落一条」，不是「接口不报错」：
两次请求拿到的单号必须相同，而且状态码要能区分「新建」和「已经存在」。

审计要证明的是「留痕，但不留凭证」：工具调用能查到，token 查不到。
"""

import json

import pytest

from agent import audit, loop
from agent.biz_client import BizClient
from agent.loop import run_turn
from agent.session import Session
from biz import tokens
from tests.conftest import A_ORDER, FakeLLM, tool_call

A_TOKEN = tokens.issue("C1001")
A_HEAD = {"Authorization": f"Bearer {A_TOKEN}"}


@pytest.fixture()
def audit_dir(tmp_path, monkeypatch):
    """审计写到临时目录，别污染仓库里的 logs/。"""
    target = tmp_path / "logs"
    monkeypatch.setattr(audit, "LOG_DIR", target)
    return target


# ── 工单幂等 ────────────────────────────────────────────────────────


def test_same_request_returns_same_ticket(client) -> None:
    body = {"reason": "键盘有两个键连击，想换货", "order_no": A_ORDER}
    first = client.post("/tickets", headers=A_HEAD, json=body)
    second = client.post("/tickets", headers=A_HEAD, json=body)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers.get("x-idempotent-replay") == "true"
    assert first.json()["ticket_no"] == second.json()["ticket_no"]


def test_repeated_submit_does_not_grow_list(client) -> None:
    body = {"reason": "包装破损", "order_no": A_ORDER}
    for _ in range(3):
        client.post("/tickets", headers=A_HEAD, json=body)
    # 种子里 C1001 已有 1 条，重复提交 3 次也只多 1 条
    assert len(client.get("/tickets", headers=A_HEAD).json()) == 2


def test_different_reason_creates_new_ticket(client) -> None:
    """幂等键含描述内容：换了说法就是另一张单，不是「同一客户只许一张」。"""
    a = client.post(
        "/tickets", headers=A_HEAD, json={"reason": "键盘按键失灵", "order_no": A_ORDER}
    )
    b = client.post(
        "/tickets", headers=A_HEAD, json={"reason": "键盘外壳有划痕", "order_no": A_ORDER}
    )
    assert a.json()["ticket_no"] != b.json()["ticket_no"]
    assert b.status_code == 201


def test_idempotency_is_per_customer(client) -> None:
    """同样的描述、同样的订单号，换个客户就是另一张单。

    如果键里只有订单号和描述、没有客户号，A 建过的单会让 B 的请求
    直接命中缓存——那是一起越权，不是幂等。
    """
    token_b = tokens.issue("C1002")
    body = {"reason": "包装破损", "order_no": "SF20261026005"}  # B 自己的单
    r = client.post("/tickets", headers={"Authorization": f"Bearer {token_b}"}, json=body)
    assert r.status_code == 201


# ── Agent 侧重复调用 ────────────────────────────────────────────────


def test_agent_sees_replay_and_does_not_retry(monkeypatch, transport, audit_dir) -> None:
    """模型重复调 create_ticket 时，第二次拿到的是「已经存在」。

    这条文案很重要：如果只说「已创建」，模型会以为又建了一张；
    说「已经存在……不要再次创建」，它下一轮才会停手。
    """
    session = Session(id="s1", token=A_TOKEN, customer_id="C1001")
    client = BizClient("http://biz", A_TOKEN, transport=transport)
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM(
            [
                tool_call("create_ticket", f'{{"reason":"键盘失灵","order_no":"{A_ORDER}"}}', "c1"),
                tool_call("create_ticket", f'{{"reason":"键盘失灵","order_no":"{A_ORDER}"}}', "c2"),
                {"role": "assistant", "content": "工单已存在"},
            ]
        ),
    )
    turn = run_turn(session, "帮我建个工单", client)

    assert "已创建工单" in turn.steps[0].result
    assert "已经存在" in turn.steps[1].result
    assert _ticket_no(turn.steps[0].result) == _ticket_no(turn.steps[1].result)


def _ticket_no(text: str) -> str:
    import re

    match = re.search(r"TK\d{8}[0-9A-F]{4}", text)
    assert match, text
    return match.group(0)


# ── 审计 ────────────────────────────────────────────────────────────


def test_audit_written_for_every_call(monkeypatch, transport, audit_dir) -> None:
    session = Session(id="s-audit", token=A_TOKEN, customer_id="C1001")
    client = BizClient("http://biz", A_TOKEN, transport=transport)
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM(
            [
                tool_call("list_my_orders", "{}", "c1"),
                tool_call("get_order", f'{{"order_no":"{A_ORDER}"}}', "c2"),
                {"role": "assistant", "content": "好的"},
            ]
        ),
    )
    run_turn(session, "我的订单", client)

    files = list(audit_dir.glob("audit-*.jsonl"))
    assert len(files) == 1
    entries = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [e["tool"] for e in entries] == ["list_my_orders", "get_order"]
    assert all(e["ok"] for e in entries)
    assert all(e["customer"] == "C1001" for e in entries)


def test_audit_never_contains_token(monkeypatch, transport, audit_dir) -> None:
    """日志会被打包、贴进工单、发到群里。token 进去一次就要全量轮换。"""
    session = Session(id="s-token", token=A_TOKEN, customer_id="C1001")
    client = BizClient("http://biz", A_TOKEN, transport=transport)
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM([tool_call("list_my_orders", "{}"), {"role": "assistant", "content": "好的"}]),
    )
    run_turn(session, "我的订单", client)

    text = "".join(p.read_text(encoding="utf-8") for p in audit_dir.glob("*.jsonl"))
    assert A_TOKEN not in text
    assert A_TOKEN.split(".")[1] not in text


def test_audit_marks_failures(monkeypatch, transport, audit_dir) -> None:
    session = Session(id="s-fail", token=A_TOKEN, customer_id="C1001")
    client = BizClient("http://biz", A_TOKEN, transport=transport)
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM(
            [
                tool_call("get_order", '{"order_no":"SF00000000000"}'),
                {"role": "assistant", "content": "查不到"},
            ]
        ),
    )
    run_turn(session, "查个不存在的单", client)

    path = next(audit_dir.glob("*.jsonl"))
    entry = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["ok"] is False
