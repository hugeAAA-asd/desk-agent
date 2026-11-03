"""政策问答：检索 + 拒答。

拒答阈值 0.3 不是拍脑袋：标定集上正样本最低 0.500、负样本最高 0.000，
取正样本下界的六成留余量。换语料要重新标定，`tests/test_policy.py` 里
留了标定用例，改了文档或阈值会立刻变红。

拒答之后不是「我不知道」就完了——客服场景里，答不了必须给出口：
告诉用户不确定，并建议转人工。这条写在工具返回值和 system prompt 两处，
因为模型可能只看其中一处。
"""

from .policy_docs import POLICIES
from .retrieval import PolicyIndex

INDEX = PolicyIndex(POLICIES)
GATE = 0.3
TOP_K = 3

NO_MATCH = (
    "知识库中没有找到相关条款。\n"
    "如果用户问的是政策细节，请如实说明你不确定，并主动建议转人工处理；"
    "不要根据常识推测天数、金额或责任划分。"
)

FOOTER = "\n\n（以上为知识库原文。回答时不要添加原文没有的条款、天数或金额。）"


def search_policy(query: str) -> str:
    """返回可读的条款原文；没有足够相关的条款时返回拒答提示。"""
    if not query.strip():
        return "错误：query 不能为空。"

    if INDEX.best_title_coverage(query) < GATE:
        return NO_MATCH

    hits = [h for h in INDEX.search(query, top_k=TOP_K) if h.title_coverage >= GATE]
    if not hits:
        return NO_MATCH

    blocks = [f"【{h.doc['id']}｜{h.doc['title']}】\n{h.doc['text']}" for h in hits]
    return "\n\n".join(blocks) + FOOTER
