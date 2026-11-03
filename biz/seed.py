"""演示数据：4 个客户、12 张订单、1 条历史工单。

固定内容、不带随机，保证 clone 下来每一次跑出来的都是同一份数据。
两个人名和单号是刻意留的：C1001 是测试主账号，C1002 是被越权的那位。
测试里「A 查不到 B 的单」用的就是这两个号。
"""

import sqlite3

from . import store

CUSTOMERS = [
    ("C1001", "张伟", "13800001001", "plus"),
    ("C1002", "李娜", "13800001002", "normal"),
    ("C1003", "王芳", "13800001003", "plus"),
    ("C1004", "刘洋", "13800001004", "normal"),
]

# (订单号, 客户, 商品, 金额, 状态, 运单号, 下单时间)
ORDERS = [
    (
        "SF20261028001",
        "C1001",
        "机械键盘 87 键 青轴",
        399.00,
        "shipped",
        "SF123456789001",
        "2026-10-28",
    ),
    (
        "SF20261015002",
        "C1001",
        "头戴式降噪耳机",
        899.00,
        "delivered",
        "SF123456789002",
        "2026-10-15",
    ),
    ("SF20261010003", "C1001", "显示器增高支架", 259.00, "paid", None, "2026-10-10"),
    (
        "SF20260928004",
        "C1001",
        "不锈钢保温杯 500ml",
        129.00,
        "refunded",
        "SF123456789004",
        "2026-09-28",
    ),
    ("SF20261026005", "C1002", "人体工学椅", 1299.00, "delivered", "SF223456789005", "2026-10-26"),
    ("SF20261018006", "C1002", "移动硬盘 2TB", 549.00, "shipped", "SF223456789006", "2026-10-18"),
    ("SF20261005007", "C1002", "护眼台灯", 199.00, "closed", "SF223456789007", "2026-10-05"),
    ("SF20261024008", "C1003", "无线鼠标", 159.00, "delivered", "SF323456789008", "2026-10-24"),
    (
        "SF20261012009",
        "C1003",
        "笔记本内胆包 14 寸",
        89.00,
        "shipped",
        "SF323456789009",
        "2026-10-12",
    ),
    ("SF20260930010", "C1003", "USB-C 扩展坞", 329.00, "refunded", "SF323456789010", "2026-09-30"),
    (
        "SF20261020011",
        "C1004",
        "手机钢化膜（两片装）",
        39.00,
        "delivered",
        "SF423456789011",
        "2026-10-20",
    ),
    ("SF20261008012", "C1004", "桌面收纳盒", 69.00, "closed", "SF423456789012", "2026-10-08"),
]

# (工单号, 客户, 关联订单, 事由, 状态, 创建时间)
TICKETS = [
    (
        "TK202610290001",
        "C1001",
        "SF20261028001",
        "键盘其中一个轴按下去没反应，想换货",
        "open",
        "2026-10-29 10:12:00",
    ),
]


def seed(force: bool = False) -> bool:
    """写入演示数据。库非空就跳过，除非 force=True。

    返回是否真的写了——调用方（启动流程 / 测试）靠返回值判断，
    不要靠「反正它会自己处理」这种假设。
    """
    if not force and not store.is_empty():
        return False

    with store.connect() as conn:
        if force:
            conn.executescript("DELETE FROM tickets; DELETE FROM orders; DELETE FROM customers;")
        conn.executemany(
            "INSERT INTO customers (id, name, phone, level) VALUES (?, ?, ?, ?)",
            CUSTOMERS,
        )
        conn.executemany(
            "INSERT INTO orders "
            "(order_no, customer_id, product, amount, status, tracking_no, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ORDERS,
        )
        conn.executemany(
            "INSERT INTO tickets (ticket_no, customer_id, order_no, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            TICKETS,
        )
    return True


def ensure_ready() -> None:
    """建表 + 空库时写种子。启动和测试都走这一个入口。"""
    store.init_db()
    seed()


__all__ = ["CUSTOMERS", "ORDERS", "TICKETS", "ensure_ready", "seed", "sqlite3"]
