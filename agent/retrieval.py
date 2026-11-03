"""政策检索：BM25 排序 + 标题覆盖率判据。

为什么不用向量检索
------------------
政策只有 24 条，问题与条款的用词高度重叠（退货、运费、几天、保修），
BM25 在这个规模上够用。更实际的原因是**依赖成本**：引 sentence-transformers
会让「clone 下来 30 秒能跑」变成「先装 2GB 的 torch」。

什么时候该换：语料上到几百条，或者用户开始说一个关键词都不含的话，
关键词检索的召回会崩。那时才值得上 embedding——rag-lab 里有完整的对照实验，
不必在这个仓库重做一遍。

为什么用「标题覆盖率」判拒答
---------------------------
BM25 的分数没有固定上限，和语料规模、文档长度强相关，换一份语料同一个阈值
就不成立。全文覆盖率也不行：实测正样本最低 0.152、负样本最高 0.146，
两者重叠，没有可用的间隙。

标题覆盖率能分开：政策问题一定落在某一条的**主题**上，而「我的订单到哪了」
虽然含「订单」二字，却不落在任何条款主题上。实测正样本 ≥ 0.2，负样本 0。

为什么要维护别名表
-----------------
「价保」匹配不上「价格保护」，「能退吗」匹配不上「退货」——这是词汇鸿沟，
靠调算法解决不了，只能建一张业务术语表。这比让模型自己猜靠谱：
术语表错了能改，模型猜错了只能重训。
"""

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
K1 = 1.5
B = 0.75

# 疑问词与功能词：它们能匹配上标题纯属巧合，不代表问题落在这一条上。
# 「你能做什么」只认识一个「什么」，「这个键盘多少钱」只认识一个「多少」，
# 不排掉的话两句话都会命中「发货时间」「包邮门槛」，覆盖率直接满分。
STOPWORDS = {
    "什么",
    "怎么",
    "如何",
    "多少",
    "哪些",
    "哪个",
    "可以",
    "能否",
    "是否",
    "你们",
    "我们",
    "我的",
    "你的",
    "请问",
    "一下",
    "这个",
    "那个",
    "多久",
    "几天",
    "时候",
    "需要",
    "应该",
    "是不",
    "不是",
    "有没",
    "没有",
    "知道",
    "告诉",
    # 通用动词/名词：任何条款里都可能出现，命中它们不代表问题落在这一条上。
    # 「支持花呗分期吗」曾因为「支持」命中「不支持退货的商品」而被误召回，
    # 模型虽然识破了，但那是模型兜底，不该让检索层漏过去。
    "支持",
    "时间",
    "问题",
    "处理",
    "方式",
    "商品",
}


def tokenize(text: str) -> list[str]:
    """中文按二元组切，英文数字按整词。

    不加单字：实测加单字后负样本「你们老板是谁」也能拿到 0.069，
    正负间隙从 +0.15 变成 -0.05，判据直接失效。单字带来的噪音
    远大于它补回来的召回。
    """
    tokens: list[str] = []
    for chunk in _TOKEN_RE.findall(text.lower()):
        if chunk.isascii() or len(chunk) == 1:
            tokens.append(chunk)
        else:
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


@dataclass
class Hit:
    score: float
    coverage: float
    title_coverage: float
    doc: dict[str, Any]


class PolicyIndex:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = docs
        # 标题 + 别名合成「主题文本」，它既是 BM25 的加权部分，也是拒答判据的
        # 命中目标。标题重复一次做加权：用户问法通常贴着标题而不是正文某句。
        self._title_text = [_title_of(d) for d in docs]
        self._title_tokens = [set(tokenize(t)) for t in self._title_text]
        self._tokens = [tokenize(f"{t} {t} {d['text']}") for t, d in zip(self._title_text, docs)]
        self._tf = [Counter(t) for t in self._tokens]
        self._len = [len(t) for t in self._tokens]
        self._avg = sum(self._len) / len(self._len) if self._len else 1.0
        self._n = len(docs)

        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        self._df = df
        self._idf = {
            word: math.log(1 + (self._n - count + 0.5) / (count + 0.5))
            for word, count in df.items()
        }
        # 没在语料里出现过的词，按「只出现在 0 篇文档」算，权重最高
        self._oov_idf = math.log(1 + (self._n + 0.5) / 0.5)

    def search(self, query: str, top_k: int = 3) -> list[Hit]:
        q = tokenize(query)
        if not q:
            return []
        hits: list[Hit] = []
        for i, doc in enumerate(self.docs):
            score = self._score(q, i)
            if score <= 0:
                continue
            hits.append(
                Hit(
                    score=score,
                    coverage=self._coverage(q, i),
                    title_coverage=self._title_coverage(q, i),
                    doc=doc,
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def best_title_coverage(self, query: str) -> float:
        """最高标题覆盖率，用来决定「该不该答」。"""
        q = tokenize(query)
        if not q:
            return 0.0
        return max((self._title_coverage(q, i) for i in range(self._n)), default=0.0)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _score(self, q_tokens: list[str], i: int) -> float:
        tf, dl = self._tf[i], self._len[i]
        total = 0.0
        for word in q_tokens:
            freq = tf.get(word)
            if not freq:
                continue
            idf = self._idf.get(word, self._oov_idf)
            total += idf * freq * (K1 + 1) / (freq + K1 * (1 - B + B * dl / self._avg))
        return total

    def _coverage(self, q_tokens: list[str], i: int) -> float:
        """全文 idf 加权覆盖率。只看「语料认识的词」，OOV 按高权重计入分母。"""
        total = 0.0
        hit = 0.0
        tf = self._tf[i]
        for word in set(q_tokens):
            idf = self._idf.get(word, self._oov_idf)
            total += idf
            if word in tf:
                hit += idf
        return hit / total if total else 0.0

    def _title_coverage(self, q_tokens: list[str], i: int) -> float:
        """问题里有多少「业务词」命中了这一条的标题（含别名）。

        三条规则，每条都是实测撞出来的：

        1. **分母只算语料里出现过的词**。长句里总有一堆没见过的词
           （「上周」「买了个」），算进分母会把正常问题稀释到阈值以下。
        2. **疑问词与功能词不算业务词**（见 STOPWORDS）。「你能做什么」只认识
           一个「什么」，命中就成了 100%——比例在分母为 1 时没有统计意义。
           先按词数挡（要求 ≥2）会误杀「发票怎么开」这种只有一个实词的短问句，
           所以改成按词性挡。
        3. **一个业务词都没命中就判 0**，不管比例多少。
        """
        known = {w for w in q_tokens if w in self._idf and w not in STOPWORDS}
        if not known:
            return 0.0
        hit = known & self._title_tokens[i]
        if not hit:
            return 0.0
        return len(hit) / len(known)


def _title_of(doc: dict[str, Any]) -> str:
    aliases = doc.get("aliases") or []
    return " ".join([doc["title"], *aliases])
