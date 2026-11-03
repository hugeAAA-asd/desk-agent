"""政策检索与拒答的标定测试。

这组测试是**阈值的数据来源**，不是事后补的断言：

- 正样本 20 条，全部要过 GATE；
- 负样本 16 条，全部要被拒；
- top1 要落在正确的条款上。

改政策文档、改别名、改阈值，这里会立刻变红——这就是它存在的意义。
阈值 0.3 取自「正样本最低 0.500」的六成，不是随手写的数。
"""

import pytest

from agent import tools
from agent.policy import GATE, NO_MATCH, search_policy
from agent.policy_docs import POLICIES
from agent.retrieval import PolicyIndex, tokenize

INDEX = PolicyIndex(POLICIES)

# (问题, 期望命中的条款)。覆盖短问句与口语长句两类真实说法。
POSITIVE = [
    ("七天无理由退货要满足什么条件", "P01"),
    ("退款多久能到账", "P04"),
    ("退货运费谁出", "P07"),
    ("多久发货", "P10"),
    ("保修期多长", "P15"),
    ("发票怎么开", "P18"),
    ("可以改收货地址吗", "P13"),
    ("价保怎么申请", "P20"),
    ("满多少包邮", "P06"),
    ("换货要几天", "P05"),
    ("赠品要退吗", "P23"),
    ("投诉多久回复", "P21"),
    ("你好，我上周买的键盘不想要了，能退吗", "P01"),
    ("这个还在保修期内吗", "P15"),
    ("你们发货快吗", "P10"),
    ("买贵了能退差价吗", "P20"),
    ("发票开错了能重开吗", "P19"),
    ("退货运费是你们出吧", "P07"),
    ("新疆发货要加钱吗", "P09"),
    ("坏了能换新的吗", "P05"),
]

# 闲聊 + 订单查询。后者虽然也含「订单」「到」这类词，但不落在任何条款主题上，
# 应该交给订单工具，而不是拿政策硬答。
NEGATIVE = [
    "今天天气怎么样",
    "帮我写一首诗",
    "你们老板是谁",
    "股票怎么买",
    "你好",
    "你能做什么",
    "帮我推荐一款耳机",
    "今天几号",
    "讲个笑话",
    "这个键盘多少钱",
    "你是谁训练的",
    "我的订单到哪了",
    "帮我查下物流",
    "SF20261028001 这个单",
    "你多大了",
    "帮我写个周报",
]


def test_positive_samples_all_pass_gate() -> None:
    scores = {q: INDEX.best_title_coverage(q) for q, _ in POSITIVE}
    failed = {q: round(s, 3) for q, s in scores.items() if s < GATE}
    assert not failed, f"这些政策问题被拒答了：{failed}（GATE={GATE}）"
    assert min(scores.values()) >= 0.5


def test_negative_samples_all_rejected() -> None:
    scores = {q: INDEX.best_title_coverage(q) for q in NEGATIVE}
    leaked = {q: round(s, 3) for q, s in scores.items() if s >= GATE}
    assert not leaked, f"这些非政策问题被当成了政策：{leaked}"
    assert max(scores.values()) == 0.0


def test_top1_lands_on_expected_clause() -> None:
    wrong = {}
    for question, expected in POSITIVE:
        hits = INDEX.search(question, top_k=1)
        got = hits[0].doc["id"] if hits else "-"
        if got != expected:
            wrong[question] = f"{got} != {expected}"
    assert not wrong, wrong


def test_gate_has_margin_between_classes() -> None:
    """阈值必须落在两类之间，不能贴着任何一边。

    贴着正样本下界 → 换个说法就漏；贴着负样本上界 → 闲聊就会被当成政策。
    """
    lowest_positive = min(INDEX.best_title_coverage(q) for q, _ in POSITIVE)
    highest_negative = max(INDEX.best_title_coverage(q) for q in NEGATIVE)
    assert highest_negative < GATE < lowest_positive
    assert lowest_positive - GATE >= 0.15


# ── 工具层行为 ──────────────────────────────────────────────────────


def test_search_policy_returns_clause_text() -> None:
    text = search_policy("七天无理由退货")
    assert "P01" in text
    assert "7 个自然日" in text
    assert "不要添加原文没有的条款" in text


def test_search_policy_refuses_out_of_scope() -> None:
    assert search_policy("今天天气怎么样") == NO_MATCH


def test_refusal_points_to_human() -> None:
    """拒答不能只说「不知道」——客服场景必须给出口。"""
    assert "转人工" in NO_MATCH


def test_alias_bridges_vocabulary_gap() -> None:
    """「价保」和「价格保护」字面不重合，靠别名表接上。

    这是词汇鸿沟，不该指望模型自己猜。
    """
    assert INDEX.best_title_coverage("价保怎么申请") >= GATE
    assert "P20" in search_policy("价保怎么申请")


def test_empty_query_rejected_by_tool() -> None:
    result = tools.execute("search_policy", {"query": "  "})
    assert "不能为空" in result or "缺少参数" in result


def test_tool_rejects_identity_argument() -> None:
    """政策工具同样不接受身份参数——白名单是所有工具共用的。"""
    result = tools.execute(
        "search_policy",
        {"query": "退货", "customer_id": "C1002"},
        client=None,
    )
    assert "不接受参数" in result


@pytest.mark.parametrize("question", ["今天天气", "帮我写诗"])
def test_tokenizer_produces_bigrams(question: str) -> None:
    tokens = tokenize(question)
    assert tokens
    assert all(len(t) <= 2 for t in tokens if not t.isascii())
