"""业务系统的 HTTP 客户端。

这是 Agent 侧唯一能碰到业务数据的地方，也是「身份注入」唯一发生的位置：
`Authorization` 头在这里拼上，往上（工具层、模型上下文）都看不到它。

注意这一层**没有 import biz 的任何东西**——只有 URL 和 JSON。
换掉 `base_url` 就能指向真实系统，这是「接系统」和「同进程假函数」的区别。
"""

from typing import Any

import httpx


class BizError(Exception):
    """业务系统返回了非 2xx。message 是能直接回灌给模型的中文说明。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class OrderNotFound(BizError):
    """订单不存在，或不属于当前客户。业务系统不区分这两种情况，这里也不区分。"""


class BizClient:
    """一个会话一个实例：token 在构造时绑定，之后没有任何方法能改它。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BizClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── 内部 ────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # 网络层错误也转成模型能读懂的话，不要让 traceback 冒到对话里
            raise BizError(0, f"业务系统连不上：{exc.__class__.__name__}") from exc

        if resp.status_code == 404:
            raise OrderNotFound(404, "订单不存在")
        if resp.status_code == 401:
            raise BizError(401, "身份已失效，请重新登录")
        if resp.status_code >= 400:
            detail = _detail(resp)
            raise BizError(resp.status_code, f"业务系统返回 {resp.status_code}：{detail}")
        return resp.json()

    # ── 对外 ────────────────────────────────────────────────────────────

    def me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def list_orders(self) -> list[dict[str, Any]]:
        return self._request("GET", "/orders")

    def get_order(self, order_no: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{order_no}")

    def create_ticket(self, reason: str, order_no: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": reason}
        if order_no:
            payload["order_no"] = order_no
        return self._request("POST", "/tickets", json=payload)


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:80]
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)[:80]
