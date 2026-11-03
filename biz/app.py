"""业务系统 HTTP 层（模拟甲方已有的售后服务系统）。

这个文件是「Agent 能碰到的边界」：对外只暴露七个端点，每个业务端点的
第一件事都是从 `Authorization` 头确定「你是谁」。请求体里**没有**
`customer_id` 这个字段——它由服务端从 token 解析后注入。

第 2 天的 Agent 通过 HTTP 调这里：它拿不到数据库连接，也拿不到别人的 token。
这就是「接系统」和「把数据塞进提示词」的分界线。
"""

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from . import seed, store, tokens

load_dotenv()  # 有 .env 就用，没有就退回系统环境变量

logger = logging.getLogger("desk.biz")


@asynccontextmanager
async def lifespan(_: FastAPI):
    seed.ensure_ready()
    if tokens.using_default_secret():
        logger.warning("DESK_SECRET 仍是开发默认值，只可在本地使用")
    yield


app = FastAPI(title="Desk 业务系统", version="0.1.0", lifespan=lifespan)


# ── 出参模型 ────────────────────────────────────────────────────────────
# 用 response_model 不只是为了生成文档：它会把多出来的字段裁掉。
# 即使哪天 store 层手滑多 SELECT 了一列（比如 phone），也漏不到响应里。
# 这是第三道防线，前两道是「SQL 带 customer_id」和「404 不区分存在性」。


class CustomerOut(BaseModel):
    id: str
    name: str
    level: str


class OrderOut(BaseModel):
    order_no: str
    product: str
    amount: float
    status: str
    status_text: str
    tracking_no: str | None
    created_at: str


class TicketIn(BaseModel):
    reason: str = Field(min_length=2, max_length=200, description="问题描述")
    order_no: str | None = Field(default=None, description="关联订单号，可空")


class TicketOut(BaseModel):
    ticket_no: str
    order_no: str | None
    reason: str
    status: str
    created_at: str


class HealthOut(BaseModel):
    status: str
    customers: int


# ── 身份依赖 ────────────────────────────────────────────────────────────


def current_customer(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """从 Bearer token 解析客户号；任何异常一律 401。

    401 带 `WWW-Authenticate: Bearer` 是 RFC 7235 的要求，
    前端和网关靠它判断「该跳登录页」。缺了这个头，很多客户端会把
    401 当成普通错误而不是「未认证」。
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    customer_id = tokens.verify(authorization[7:].strip())
    if customer_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return customer_id


Customer = Annotated[str, Depends(current_customer)]


# ── 端点 ────────────────────────────────────────────────────────────────


@app.get("/healthz", response_model=HealthOut)
def healthz() -> HealthOut:
    """探活。不带 token，也不泄露任何客户数据——只有一个计数。"""
    return HealthOut(status="ok", customers=len(seed.CUSTOMERS))


@app.get("/me", response_model=CustomerOut)
def me(customer_id: Customer) -> CustomerOut:
    customer = store.get_customer(customer_id)
    if customer is None:
        # token 签名对、但客户已被删：同样是 401，不区分「令牌伪造」和
        # 「账号已注销」，避免变成账号枚举接口。
        raise HTTPException(status_code=401, detail="账号不可用")
    return CustomerOut(**customer)


@app.get("/orders", response_model=list[OrderOut])
def list_orders(customer_id: Customer) -> list[OrderOut]:
    """我的订单列表。没有「查全部」的参数，翻页也不给越界的机会。"""
    return [
        OrderOut(**o, status_text=store.ORDER_STATUS.get(o["status"], o["status"]))
        for o in store.list_orders(customer_id)
    ]


@app.get("/orders/{order_no}", response_model=OrderOut)
def get_order(order_no: str, customer_id: Customer) -> OrderOut:
    """按单号查单。

    别人的单返回 404 而不是 403。理由：403 等于承认「这个单号存在，
    只是不属于你」，攻击者可以用它批量验证单号是否有效。404 把
    「不存在」和「不是你的」压成同一个响应，泄露的信息最少。
    """
    try:
        order = store.get_order(customer_id, order_no)
    except store.OrderNotFound:
        raise HTTPException(status_code=404, detail="订单不存在") from None
    return OrderOut(**order, status_text=store.ORDER_STATUS.get(order["status"], order["status"]))


@app.get("/tickets", response_model=list[TicketOut])
def list_tickets(customer_id: Customer) -> list[TicketOut]:
    return [TicketOut(**t) for t in store.list_tickets(customer_id)]


@app.post("/tickets", response_model=TicketOut, status_code=201)
def create_ticket(body: TicketIn, response: Response, customer_id: Customer) -> TicketOut:
    """建工单。关联订单时同样做归属校验——写入侧是越权的第二道关。

    重复提交返回 200 而不是 201，并带 `X-Idempotent-Replay: true`。
    区分状态码是给调用方看的：201 是「真的建了」，200 是「已经有了」。
    两者都返回同一张工单，调用方拿到的单号不会变。
    """
    try:
        ticket, replay = store.create_ticket(customer_id, body.reason, body.order_no)
    except store.OrderNotFound:
        raise HTTPException(status_code=404, detail="订单不存在") from None

    if replay:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Idempotent-Replay"] = "true"
    return TicketOut(**ticket)


# 这里**故意没有**「签发 token」的端点。
#
# 业务系统只验签、不签发：签发属于登录服务的职责，两者混在一个进程里，
# 等于业务端多了一个能伪造任意身份的入口——认证和业务就被放进了同一个
# 信任域。本地要 token 用 `python -m scripts.mint_token C1001`，
# 那是一段离线脚本，跑不跑得起来取决于你有没有仓库的读权限。
