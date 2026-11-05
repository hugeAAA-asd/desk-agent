"""订单汇总查询的测试。

这一组盯的是 JOIN 最容易出错的地方：**一对多连接会让左表的行重复**。
一个订单挂两张工单时，如果直接 `LEFT JOIN tickets` 再 `COUNT(*)`，
订单数会变成 2、金额会加两遍——不报错，只是数字静默偏大。

所以除了「算得对不对」，还要专门测「一个订单多张工单时不会重复计数」。
"""

from biz import tokens

A = tokens.issue("C1001")
B = tokens.issue("C1002")

A_HEAD = {"Authorization": f"Bearer {A}"}
B_HEAD = {"Authorization": f"Bearer {B}"}

# C1001 名下 4 单（见 biz/seed.py）：
#   SF20261028001 机械键盘 399.00 shipped
#   SF20261015002 降噪耳机 899.00 delivered
#   SF20261010003 增高支架 259.00 paid
#   SF20260928004 保温杯   129.00 refunded
A_ORDER = "SF20261028001"
A_TOTAL = 399.00 + 899.00 + 259.00 + 129.00


def test_summary_needs_token(client) -> None:
    assert client.get("/orders/summary").status_code == 401


def test_summary_groups_by_status(client) -> None:
    rows = client.get("/orders/summary", headers=A_HEAD).json()

    # C1001 四个状态各一单
    assert len(rows) == 4
    assert sum(r["order_count"] for r in rows) == 4
    assert round(sum(r["total_amount"] for r in rows), 2) == A_TOTAL
    # 状态码要翻译成中文，前端直接用
    assert all(r["status_text"] for r in rows)


def test_summary_order_by_amount_desc(client) -> None:
    rows = client.get("/orders/summary", headers=A_HEAD).json()
    amounts = [r["total_amount"] for r in rows]
    assert amounts == sorted(amounts, reverse=True)


def test_summary_has_no_ticket_when_none_created(client) -> None:
    """没提过工单的订单也要出现在汇总里——这是 LEFT JOIN 而非 INNER JOIN 的意义。

    INNER JOIN 会把「没工单的订单」整行丢掉，汇总就只剩提过工单的那些。
    种子里只有 SF20261028001（shipped）有一张工单，另外三单都没有。
    """
    rows = client.get("/orders/summary", headers=A_HEAD).json()
    assert len(rows) == 4, "四个状态都要在，没工单的不能被丢掉"

    shipped = next(r for r in rows if r["status"] == "shipped")
    assert shipped["ticket_count"] == 1, "种子给这单建过一张工单"

    others = [r for r in rows if r["status"] != "shipped"]
    assert all(r["ticket_count"] == 0 for r in others), "这三单没有工单，必须是 0 而不是被丢掉"


def test_summary_does_not_double_count_multi_ticket_order(client) -> None:
    """同一订单再加两张工单，订单数和金额都不许翻倍。

    幂等键是「客户|单号|描述」，描述不同就是新工单，所以这里能造出
    「一个订单三张工单」（种子 1 张 + 新建 2 张）。如果实现是朴素的
    `LEFT JOIN + COUNT(*)`，这条会红：order_count 变 3、金额变 1197。
    """
    for reason in ("键盘轴体失灵", "申请换新"):
        r = client.post("/tickets", headers=A_HEAD, json={"reason": reason, "order_no": A_ORDER})
        assert r.status_code == 201, r.text

    rows = client.get("/orders/summary", headers=A_HEAD).json()
    shipped = next(r for r in rows if r["status"] == "shipped")

    assert shipped["order_count"] == 1, "一个订单只能算一单"
    assert shipped["total_amount"] == 399.00, "金额不能被工单数放大"
    assert shipped["ticket_count"] == 3, "种子 1 张 + 新建 2 张"


def test_summary_does_not_leak_other_customer(client) -> None:
    """汇总同样受身份约束：B 看不到 A 的任何订单和金额。"""
    rows = client.get("/orders/summary", headers=B_HEAD).json()
    assert sum(r["order_count"] for r in rows) == 3
    # C1002 的三单：1299 + 549 + 199
    assert round(sum(r["total_amount"] for r in rows), 2) == 2047.00
