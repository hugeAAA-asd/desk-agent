"""接口层测试：认证、会话、以及响应里不能出现 token。"""

import pytest
from fastapi.testclient import TestClient

from agent import api, loop
from agent.biz_client import BizClient
from agent.session import store
from biz import tokens
from tests.conftest import FakeLLM, tool_call

A_TOKEN = tokens.issue("C1001")
A_HEAD = {"Authorization": f"Bearer {A_TOKEN}"}


@pytest.fixture()
def api_client(transport, monkeypatch):
    store._sessions.clear()
    monkeypatch.setattr(
        api,
        "_client_factory",
        lambda token: BizClient("http://biz", token, transport=transport),
    )
    with TestClient(api.app) as test_client:
        yield test_client
    store._sessions.clear()


def test_chat_requires_token(api_client) -> None:
    r = api_client.post("/chat", json={"message": "你好"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_chat_rejects_forged_token(api_client) -> None:
    """Agent 不自己验签，但会拿着 token 去问业务系统——假令牌在这里就被挡掉。"""
    r = api_client.post(
        "/chat",
        json={"message": "你好"},
        headers={"Authorization": "Bearer C1001.0000000000000000"},
    )
    assert r.status_code == 401


def test_chat_returns_reply_and_steps(api_client, monkeypatch) -> None:
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM(
            [
                tool_call("list_my_orders", "{}"),
                {"role": "assistant", "content": "你一共有 4 单。"},
            ]
        ),
    )
    r = api_client.post("/chat", json={"message": "我买了什么"}, headers=A_HEAD)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"]
    assert body["reply"] == "你一共有 4 单。"
    assert body["steps"][0]["tool"] == "list_my_orders"
    assert "共 4 单" in body["steps"][0]["result"]


def test_second_message_reuses_session(api_client, monkeypatch) -> None:
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM([{"role": "assistant", "content": "好的"}]),
    )
    first = api_client.post("/chat", json={"message": "你好"}, headers=A_HEAD).json()
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM([{"role": "assistant", "content": "在的"}]),
    )
    second = api_client.post(
        "/chat",
        json={"message": "还在吗", "session_id": first["session_id"]},
        headers=A_HEAD,
    )
    assert second.status_code == 200
    assert second.json()["session_id"] == first["session_id"]


def test_unknown_session_404(api_client) -> None:
    r = api_client.post(
        "/chat",
        json={"message": "你好", "session_id": "nope"},
        headers=A_HEAD,
    )
    assert r.status_code == 404


def test_response_never_contains_token(api_client, monkeypatch) -> None:
    """响应体会被前端存进 localStorage、贴进 issue、发到群里。

    token 出现在任何一层响应里都算泄露。
    """
    monkeypatch.setattr(
        loop,
        "call_llm",
        FakeLLM([tool_call("list_my_orders", "{}"), {"role": "assistant", "content": "4 单"}]),
    )
    r = api_client.post("/chat", json={"message": "我的订单"}, headers=A_HEAD)
    assert A_TOKEN not in r.text
    assert A_TOKEN.split(".")[1] not in r.text


def test_message_length_validated(api_client) -> None:
    r = api_client.post("/chat", json={"message": ""}, headers=A_HEAD)
    assert r.status_code == 422
