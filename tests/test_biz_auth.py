"""业务系统的认证与越权测试。

这一组是第 1 天的验收标准：不带 token 401、A 拿自己的 token 查不到 B 的单、
篡改 token 无效。功能少几项没关系，这几条错了整个项目就没有意义。
"""

from biz import tokens

A = tokens.issue("C1001")
B = tokens.issue("C1002")

A_HEAD = {"Authorization": f"Bearer {A}"}
B_HEAD = {"Authorization": f"Bearer {B}"}

# C1001 名下 4 单，C1002 名下 3 单（见 biz/seed.py）
A_ORDER = "SF20261028001"
B_ORDER = "SF20261026005"


def test_healthz_needs_no_token(client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_orders_without_token_401(client) -> None:
    r = client.get("/orders")
    assert r.status_code == 401
    # 401 必须带 WWW-Authenticate，客户端靠它决定跳登录
    assert r.headers["www-authenticate"] == "Bearer"


def test_bad_scheme_401(client) -> None:
    r = client.get("/orders", headers={"Authorization": A})
    assert r.status_code == 401


def test_garbage_token_401(client) -> None:
    r = client.get("/orders", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401


def test_tampered_signature_401(client) -> None:
    forged = f"C1001.{'0' * 16}"
    r = client.get("/orders", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_swapping_customer_id_breaks_signature(client) -> None:
    """把 B 的签名接到 A 的客户号上，必须失败。

    如果签名只对「客户号」做摘要而不校验，这里就会变成
    「任意人拿到任意 token 就能改成别人的号」。
    """
    signature = B.rpartition(".")[2]
    forged = f"C1001.{signature}"
    r = client.get("/orders", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


def test_me_returns_own_profile(client) -> None:
    r = client.get("/me", headers=A_HEAD)
    assert r.status_code == 200
    assert r.json() == {"id": "C1001", "name": "张伟", "level": "plus"}


def test_orders_returns_only_own(client) -> None:
    r = client.get("/orders", headers=A_HEAD)
    assert r.status_code == 200
    order_nos = {o["order_no"] for o in r.json()}
    assert len(order_nos) == 4
    assert B_ORDER not in order_nos


def test_get_own_order_200(client) -> None:
    r = client.get(f"/orders/{A_ORDER}", headers=A_HEAD)
    assert r.status_code == 200
    assert r.json()["product"] == "机械键盘 87 键 青轴"
    assert r.json()["status_text"] == "已发货"


def test_get_other_customer_order_404(client) -> None:
    r = client.get(f"/orders/{B_ORDER}", headers=A_HEAD)
    assert r.status_code == 404


def test_forbidden_and_missing_are_indistinguishable(client) -> None:
    """这两条响应的状态码和响应体必须完全一致。

    只要有一点差别（403 vs 404、文案不同、耗时差一个数量级），
    攻击者就能拿它批量探测「哪些单号是真的」。
    """
    forbidden = client.get(f"/orders/{B_ORDER}", headers=A_HEAD)
    missing = client.get("/orders/SF00000000000", headers=A_HEAD)
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()


def test_response_cannot_leak_other_customer(client) -> None:
    """整段响应里不该出现 B 的姓名、手机号、客户号。"""
    body = client.get("/orders", headers=A_HEAD).text
    for secret in ("李娜", "13800001002", "C1002"):
        assert secret not in body


def test_create_ticket_for_own_order(client) -> None:
    r = client.post(
        "/tickets",
        headers=A_HEAD,
        json={"reason": "键盘有两个键连击，想换货", "order_no": A_ORDER},
    )
    assert r.status_code == 201
    assert r.json()["ticket_no"].startswith("TK")
    assert r.json()["status"] == "open"


def test_create_ticket_for_other_order_404(client) -> None:
    """写入侧的越权：拿到别人的单号也不能把工单挂到别人名下。"""
    r = client.post(
        "/tickets",
        headers=A_HEAD,
        json={"reason": "这个单有问题", "order_no": B_ORDER},
    )
    assert r.status_code == 404
    # 没写进去
    assert client.get("/tickets", headers=B_HEAD).json() == []


def test_tickets_are_scoped_to_owner(client) -> None:
    client.post("/tickets", headers=A_HEAD, json={"reason": "包装破损"})
    a_tickets = client.get("/tickets", headers=A_HEAD).json()
    b_tickets = client.get("/tickets", headers=B_HEAD).json()
    assert len(a_tickets) == 2  # 种子里已有 1 条 + 刚建的 1 条
    assert b_tickets == []


def test_reason_length_validated(client) -> None:
    r = client.post("/tickets", headers=A_HEAD, json={"reason": "x"})
    assert r.status_code == 422
