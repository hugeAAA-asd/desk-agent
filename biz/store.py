"""业务数据访问层（模拟甲方已有的订单 / 工单系统）。

一条硬规则
----------
每个查询函数都必须带 `customer_id`，且 SQL 里必须出现 `WHERE customer_id = ?`。
这一层**没有**「查全部订单」的入口。

越权不是靠上层记得加条件挡住的，是靠这里根本没有那条路。
第 2 天的 Agent 工具会包在这层之上：模型能表达的只有「查哪个单号」，
「查谁的单」由服务端从 token 注入——两件事分开做，缺一个都能被绕过。
"""

import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("DESK_DB", BASE_DIR / "data" / "desk.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,
    level      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no    TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    product     TEXT NOT NULL,
    amount      REAL NOT NULL,
    status      TEXT NOT NULL,
    tracking_no TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no   TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    order_no    TEXT,
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON tickets(customer_id);
"""

# 订单状态取值：paid 已付款 / shipped 已发货 / delivered 已签收 /
# refunded 已退款 / closed 已关闭。答复里说「到哪了」靠的是这张表，
# 不是模型猜。
ORDER_STATUS = {
    "paid": "已付款，待发货",
    "shipped": "已发货",
    "delivered": "已签收",
    "refunded": "已退款",
    "closed": "已关闭",
}


class OrderNotFound(Exception):
    """订单不存在，或不属于当前客户。

    两种情况共用一个异常，是故意的：上层一律转 404。
    如果「存在但没权限」返回 403、「不存在」返回 404，等于告诉攻击者
    「这个单号是真的，你只是没权限查」——这本身就是信息泄露。
    """


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """每次操作用独立连接：FastAPI 的同步端点跑在线程池里，
    共享一个连接会撞 `check_same_thread`，而加锁又把并发读退化成串行。
    SQLite 开 WAL 后多读一写可以并行，代价只是多几次 open()。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """建表。幂等，可重复调用。"""
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)


def is_empty() -> bool:
    """库里有数据吗——用于决定要不要写种子。"""
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM customers").fetchone()
    return row["n"] == 0


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cursor.fetchall()]


def get_customer(customer_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, name, phone, level FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
    return dict(row) if row else None


def list_orders(customer_id: str) -> list[dict[str, Any]]:
    """只返回该客户的订单。注意参数里没有「可选」——不传就调不了。"""
    with connect() as conn:
        cur = conn.execute(
            "SELECT order_no, product, amount, status, tracking_no, created_at "
            "FROM orders WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        # 必须在 with 内取完：离开 with 后连接已关闭，
        # cursor 还持有引用但一 fetch 就抛「closed database」。
        return _rows(cur)


def get_order(customer_id: str, order_no: str) -> dict[str, Any]:
    """按单号查，但必须同时匹配 customer_id；查不到抛 OrderNotFound。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT order_no, product, amount, status, tracking_no, created_at "
            "FROM orders WHERE customer_id = ? AND order_no = ?",
            (customer_id, order_no),
        ).fetchone()
    if row is None:
        raise OrderNotFound(order_no)
    return dict(row)


def list_tickets(customer_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.execute(
            "SELECT ticket_no, order_no, reason, status, created_at "
            "FROM tickets WHERE customer_id = ? ORDER BY created_at DESC",
            (customer_id,),
        )
        return _rows(cur)


def create_ticket(customer_id: str, reason: str, order_no: str | None = None) -> dict[str, Any]:
    """建工单。

    如果指定了订单号，先确认这单属于当前客户——否则模型只要拿到别人的
    单号，就能把工单挂到别人名下。这是越权的第二道关：第一道在查询，
    第二道在写入。
    """
    if order_no:
        get_order(customer_id, order_no)  # 不属于自己会抛 OrderNotFound

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ticket_no = f"TK{datetime.now():%Y%m%d}{uuid.uuid4().hex[:4].upper()}"

    with connect() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_no, customer_id, order_no, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?)",
            (ticket_no, customer_id, order_no, reason, now),
        )
    return {
        "ticket_no": ticket_no,
        "order_no": order_no,
        "reason": reason,
        "status": "open",
        "created_at": now,
    }
