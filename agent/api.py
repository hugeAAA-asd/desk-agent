"""对外接口：一个 /chat。

认证的处理方式值得单独说一句：**Agent 不自己验签。**

它把用户带来的 token 原样交给业务系统，第一次调用 `/me` 时由业务系统判定
有效性。这样做的好处是：
- Agent 进程里没有签名密钥，被拿下也伪造不出身份；
- 密钥轮换、账号注销这些事只在业务系统一处生效，不会出现两边判断不一致；
- 越权的判定权也留在业务系统，Agent 永远只是一个「带 token 的调用方」。

代价是每次建会话多一次 HTTP 往返。这个代价比「两套认证逻辑各写一遍」便宜。
"""

from typing import Annotated, Any, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from .biz_client import BizClient, BizError
from .config import BIZ_BASE_URL
from .loop import run_turn
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


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, token: Token) -> ChatOut:
    session: Session | None = None
    if body.session_id:
        session = store.get(body.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在或已过期")

    client = _client_factory(token)
    try:
        if session is None:
            # 新会话：先让业务系统认一次人。token 无效在这里就挡掉，
            # 不用等到模型调工具才发现。
            try:
                client.me()
            except BizError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=exc.message,
                    headers={"WWW-Authenticate": "Bearer"},
                ) from None
            session = store.create(token)

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


@app.delete("/sessions/{session_id}")
def drop_session(session_id: str) -> dict[str, bool]:
    store.drop(session_id)
    return {"dropped": True}
