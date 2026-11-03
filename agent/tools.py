"""模型能调用的三个工具。

这份 schema 里**没有 customer_id**，不是漏写。

这是整个项目的设计：模型只能表达「查什么」（单号），「查谁」由 Session 从
登录令牌解析后注入。工具函数签名里也没有这个参数——不是靠提示词叮嘱模型
「不要传别人的 id」，是**结构上不存在这个入口**。

提示词能挡住嘴，挡不住手。白名单能挡住手，挡不住「用已有信息自己算」。
所以身份这一维干脆不交给模型：它连表达的机会都没有。
"""

import json
from typing import Any

from .biz_client import BizClient, BizError, OrderNotFound
from .policy import search_policy

TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": (
                "检索售后政策条款：退换货条件、运费承担、发货与快递时效、保修、发票、"
                "价保、投诉渠道等。问规则、政策、天数、费用、责任划分之前必须先查，"
                "不要凭记忆回答。返回「没有找到相关条款」说明知识库不覆盖这个问题。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查的政策问题，用用户原话即可",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_orders",
            "description": (
                "列出当前用户自己的全部订单（单号、商品、金额、状态、物流单号）。"
                "不需要任何参数。用户问「我买了什么」「我的订单」「最近买的东西」时用它。"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": (
                "按订单号查一张订单的详情与物流状态。只能查当前用户自己的订单；"
                "单号不属于本人时返回「订单不存在」，不要据此猜测它属于别人。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_no": {
                        "type": "string",
                        # 示例必须用不存在的号：schema 会进模型上下文，写真实单号
                        # 等于把某个客户的订单号告诉所有用户。评测里模型就拿它
                        # 当例子报给了另一个客户（x2）。
                        "description": "订单号，14 位，SF 开头，如 SF20260101000",
                    }
                },
                "required": ["order_no"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": (
                "创建售后工单并转人工。用户明确要求投诉、退换货、或问题自助解决不了时使用。"
                "只在真的需要人工介入时调用，不要每个问题都建单。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "问题描述，2~200 字"},
                    "order_no": {
                        "type": "string",
                        "description": "关联的订单号；用户没提具体订单时可以不给",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

# 每个工具允许的参数。执行器会拿它做白名单，多一个都拒。
ALLOWED_ARGS: dict[str, set[str]] = {
    "search_policy": {"query"},
    "list_my_orders": set(),
    "get_order": {"order_no"},
    "create_ticket": {"reason", "order_no"},
}

# 这几个名字一旦出现在参数里，说明模型在试图自己指定身份。
# 不是「忽略掉继续跑」——那样模型会以为自己成功了，下一轮接着传。
# 必须明确告诉它「这个维度不归你管」，它才会改。
IDENTITY_KEYS = {"customer_id", "user_id", "customer", "uid", "user"}


def execute(name: str, args: dict[str, Any], client: BizClient | None = None) -> str:
    """执行一次工具调用，返回给模型看的文本。

    任何异常都转成文字回灌，不抛给循环：模型看到「订单不存在」会换个问法，
    看到 traceback 只会道歉。

    `client` 可以为 None：政策检索不碰业务数据，单元测试里不需要起业务系统。
    """
    if name not in ALLOWED_ARGS:
        return f"错误：没有名为 {name} 的工具。"

    extra = set(args) - ALLOWED_ARGS[name]
    if extra & IDENTITY_KEYS:
        bad = sorted(extra & IDENTITY_KEYS)
        return (
            f"错误：工具 {name} 不接受参数 {bad}。"
            "当前用户身份由系统从登录令牌确定，你只需要提供订单号或问题描述；"
            "不属于当前用户的订单会返回「订单不存在」。"
        )
    if extra:
        return f"错误：工具 {name} 不接受参数 {sorted(extra)}。"

    # 政策检索不碰业务数据，也不依赖 client，放在 try 外：
    # 它不会抛 BizError，混进下面的 except 只会让错误处理看起来比实际复杂。
    if name == "search_policy":
        query = str(args.get("query") or "").strip()
        if not query:
            return "错误：缺少参数 query。"
        return search_policy(query)

    if client is None:
        return "错误：这次调用缺少业务系统连接，无法访问订单。"

    try:
        if name == "list_my_orders":
            return _format_orders(client.list_orders())
        if name == "get_order":
            order_no = args.get("order_no")
            if not order_no:
                return "错误：缺少参数 order_no。"
            return _format_order(client.get_order(order_no))
        if name == "create_ticket":
            reason = (args.get("reason") or "").strip()
            if len(reason) < 2:
                return "错误：reason 太短，请让用户补充问题描述。"
            ticket, replay = client.create_ticket(reason, args.get("order_no") or None)
            if replay:
                # 明确告诉模型「没重复建」，否则它可能以为失败又试一次
                return (
                    f"工单 {ticket['ticket_no']} 已经存在（同一问题重复提交，未重复创建），"
                    f"状态：待处理。请把这个工单号告诉用户，不要再次创建。"
                )
            return (
                f"已创建工单 {ticket['ticket_no']}（状态：待处理），"
                f"关联订单：{ticket['order_no'] or '无'}。请把工单号告诉用户。"
            )
    except OrderNotFound:
        return "订单不存在。当前用户名下没有这个单号，不要猜测它属于别人。"
    except BizError as exc:
        return f"调用失败：{exc.message}"

    return "错误：工具未实现。"


def _format_order(order: dict[str, Any]) -> str:
    return json.dumps(
        {
            "订单号": order["order_no"],
            "商品": order["product"],
            "金额": order["amount"],
            "状态": order["status_text"],
            "物流单号": order["tracking_no"] or "未发货",
            "下单时间": order["created_at"],
        },
        ensure_ascii=False,
    )


def _format_orders(orders: list[dict[str, Any]]) -> str:
    """列表压成「一行一单」，比 JSON 数组省 token，模型也更好读。"""
    if not orders:
        return "当前用户名下没有订单。"
    lines = [f"共 {len(orders)} 单："]
    for order in orders:
        lines.append(
            f"- {order['order_no']}｜{order['product']}｜{order['amount']} 元"
            f"｜{order['status_text']}｜物流 {order['tracking_no'] or '未发货'}"
            f"｜{order['created_at']}"
        )
    return "\n".join(lines)
