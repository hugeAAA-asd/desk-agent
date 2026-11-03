"""测试夹具：每个用例一个独立的临时库，互不污染。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from biz import app as app_module
from biz import seed, store

A_ID = "C1001"
B_ID = "C1002"
A_ORDER = "SF20261028001"  # 属于 C1001
B_ORDER = "SF20261026005"  # 属于 C1002


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把数据层指到临时文件，再起一个应用实例。

    必须用 `with TestClient(...)`：lifespan 里才建表和写种子，
    直接 `TestClient(app)` 拿到的是一个空库。
    """
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "desk.db")
    seed.ensure_ready()
    with TestClient(app_module.app) as test_client:
        yield test_client


@pytest.fixture()
def biz(client):
    """业务系统实例的别名。Agent 侧测试里叫 biz 比 client 清楚。"""
    return client


@pytest.fixture()
def transport(biz):
    """把 httpx 的请求转交给业务系统的 TestClient。

    这样 BizClient 走的是**真实的 httpx 调用链 + 真实的鉴权依赖**，
    只是省掉 socket。比 mock 掉 BizClient 的方法可信得多：
    后者会把「Authorization 头有没有带上」这类 bug 一起屏蔽掉。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        resp = biz.request(
            request.method,
            request.url.path,
            headers=headers,
            content=request.content,
        )
        return httpx.Response(
            status_code=resp.status_code,
            headers={"content-type": resp.headers.get("content-type", "application/json")},
            content=resp.content,
        )

    return httpx.MockTransport(handler)


class FakeLLM:
    """按剧本返回 assistant message，并记录每次收到的上下文。

    越权能不能拦住这件事是确定性的，不该让真模型的随机性掺进来。
    """

    def __init__(self, script: list[dict]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, messages, tools=None, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        if self.script:
            return self.script.pop(0)
        return {"role": "assistant", "content": "好的。"}


def tool_call(name: str, args: str, call_id: str = "call_1") -> dict:
    """构造一条「模型要调工具」的 assistant 消息。"""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }
