"""对外接口：一个 /chat。

认证的处理方式值得单独说一句：**Agent 不自己验签。**

它把用户带来的 token 原样交给业务系统，第一次调用 `/me` 时由业务系统判定
有效性。这样做的好处是：
- Agent 进程里没有签名密钥，被拿下也伪造不出身份；
- 密钥轮换、账号注销这些事只在业务系统一处生效，不会出现两边判断不一致；
- 越权的判定权也留在业务系统，Agent 永远只是一个「带 token 的调用方」。

代价是每次建会话多一次 HTTP 往返。这个代价比「两套认证逻辑各写一遍」便宜。
"""

import json
from collections.abc import Iterator
from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .biz_client import BizClient, BizError
from .config import BASE_DIR, BIZ_BASE_URL
from .llm import LLMError
from .loop import run_turn, run_turn_stream
from .session import Session, store


def _default_factory(token: str) -> BizClient:
    return BizClient(BIZ_BASE_URL, token)


# 客户端工厂做成模块级可替换的变量，而不是 app.state：
# 它是测试注入点，不是应用状态，挂在 app 上反而要在测试里猜 Starlette 的
# State 语义。测试里 `monkeypatch.setattr(api, "_client_factory", ...)` 即可。
_client_factory: Callable[[str], BizClient] = _default_factory


app = FastAPI(title="Desk Agent", version="0.1.0")


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    session_id: str | None = Field(default=None, description="不传则新建会话")


class StepOut(BaseModel):
    tool: str
    args: dict[str, Any]
    result: str


class ChatOut(BaseModel):
    session_id: str
    reply: str
    steps: list[StepOut]
    rounds: int
    truncated: bool


class HealthOut(BaseModel):
    status: str
    sessions: int


def bearer_token(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="令牌为空")
    return token


Token = Annotated[str, Depends(bearer_token)]


@app.get("/healthz", response_model=HealthOut)
def healthz() -> HealthOut:
    return HealthOut(status="ok", sessions=store.count())


def _prepare(body: ChatIn, token: str) -> tuple[Session, BizClient]:
    """取会话、建客户端、必要时让业务系统认一次人。

    两个端点共用这一段：认证逻辑写两遍迟早不一致，而不一致的后果是
    「流式能进、普通不能进」这种最难查的现象。
    """
    session = store.get(body.session_id) if body.session_id else None
    if body.session_id and session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期")

    client = _client_factory(token)
    if session is not None:
        return session, client

    try:
        me = client.me()
    except BizError as exc:
        client.close()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    return store.create(token, customer_id=str(me.get("id") or "")), client


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, token: Token) -> ChatOut:
    session, client = _prepare(body, token)
    try:
        turn = run_turn(session, body.message, client)
    finally:
        client.close()

    return ChatOut(
        session_id=session.id,
        reply=turn.reply,
        steps=[StepOut(tool=s.tool, args=s.args, result=s.result) for s in turn.steps],
        rounds=turn.rounds,
        truncated=turn.truncated,
    )


@app.post("/chat/stream")
def chat_stream(body: ChatIn, token: Token) -> StreamingResponse:
    """SSE 版本：工具调用以 `step` 事件推、正文以 `delta` 事件推、最后 `done`。

    用 POST 而不是 EventSource：浏览器原生的 EventSource 只支持 GET，
    带不了请求体和 Authorization 头。前端用 fetch + ReadableStream 读，
    代价是要自己解析 `data:` 行，收益是不用把消息塞进 URL。
    """
    session, client = _prepare(body, token)

    def events() -> Iterator[str]:
        try:
            for event in run_turn_stream(session, body.message, client):
                if event.get("type") == "done":
                    # 会话 id 属于传输层，不让 loop 去关心；但它必须随 done 下发，
                    # 否则前端第二轮拿不到 id，多轮对话就断了。
                    event["session_id"] = session.id
                yield _sse(event)
        except LLMError as exc:
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            client.close()
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 默认会把响应攒够一个 buffer 再发，流式就变成「等 10 秒一次吐完」
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.delete("/sessions/{session_id}")
def drop_session(session_id: str) -> dict[str, bool]:
    store.drop(session_id)
    return {"dropped": True}


# 演示页和接口同源，省掉 CORS：跨域配置一旦写宽（allow_origins=["*"]），
# 带着 Authorization 的请求就能被任意站点发起，等于把 token 交出去。
_WEB_DIR = BASE_DIR / "web"
if _WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=_WEB_DIR, html=True), name="ui")
