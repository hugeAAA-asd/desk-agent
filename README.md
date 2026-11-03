# desk-agent

把检索和工具执行接进一套**已经存在的业务系统**，重点不是「会聊天」，是**身份边界**。

> 当前进度：业务系统（订单 / 工单 + 令牌认证）已就绪，Agent 尚未接入。

## 为什么不是又一个客服机器人

套壳客服的通病是：把 FAQ 塞进向量库，模型看到什么答什么。它答不出「我上周买的键盘到哪了」，
因为这句话的答案在一张表里，不在文档里；它更答不出「别人的订单我能查吗」，
因为项目里根本没有「别人」这个概念。

这个仓库要解决的就是后一个问题：**用户身份由服务端注入，模型只能表达「查什么」，不能表达「查谁」。**

只要模型的工具参数里出现 `customer_id`，整套权限设计就作废了——面试官第一问就会是
「我改个参数是不是能查别人的单」。所以第一天的活不是写 Agent，是把这条线划出来。

## 30 秒验证越权拦得住

```bash
pip install -r requirements.txt
python -m uvicorn biz.app:app --port 8000

# 另开一个终端
python -m scripts.mint_token C1001 C1002      # 签两个号：A 和 B
A=C1001.xxxxxxxxxxxxxxxx                      # 换成上面打印的值
B=C1002.yyyyyyyyyyyyyyyy

curl -s localhost:8000/orders                                  # 401，没令牌
curl -s -H "Authorization: Bearer $A" localhost:8000/orders     # 200，只有 A 的 4 单
curl -s -H "Authorization: Bearer $A" localhost:8000/orders/SF20261026005   # 404，那是 B 的单
curl -s -H "Authorization: Bearer $A" localhost:8000/orders/SF99999999999   # 404，响应一字不差
```

最后两条**必须长得完全一样**。只要一个是 403、一个是 404，攻击者就能拿它批量探测
「哪些单号是真的」——这本身就是信息泄露。测试 `test_forbidden_and_missing_are_indistinguishable`
就是在钉这一条。

## 三道防线

越权不是靠某一层「记得加条件」挡住的，三层各管一段，缺一层都能被绕。

| # | 在哪 | 挡什么 | 代码 |
|---|---|---|---|
| 1 | SQL 层 | 没有「查全部」的入口，每个查询都强制 `WHERE customer_id = ?` | `biz/store.py` |
| 2 | 写入层 | 给别人的单建工单同样 404（先校验归属再插入） | `store.create_ticket` |
| 3 | 响应层 | `response_model` 裁掉多余字段，手滑多 SELECT 一列也漏不出去 | `biz/app.py` |

另外两个细节：

- **令牌只装「你是谁」**，不装任何业务数据，用 HMAC 签名，校验走 `hmac.compare_digest`（防时序攻击）。
- **业务系统只验签、不签发**。签发放在离线脚本里，因为登录和业务混在一个进程，等于业务端多了一个
  伪造任意身份的入口。

## 目录

```
biz/
  app.py      FastAPI：7 个端点，身份一律从 Authorization 头解析
  store.py    数据访问层：每个函数都必须带 customer_id
  seed.py     固定演示数据：4 个客户 / 12 张订单 / 1 条工单
  tokens.py   HMAC 令牌签发与校验
scripts/
  mint_token.py   离线签令牌（给 curl 和越权测试用）
tests/
  test_biz_auth.py  16 项：401、篡改、换号、越权读写、响应不可区分
```

## 验证

```bash
pytest          # 16 passed
ruff check . && ruff format --check .
```

其中三条是这套设计的核心，不是凑数：

- `test_swapping_customer_id_breaks_signature`：拿 B 的签名拼 A 的客户号 → 401
- `test_get_other_customer_order_404`：A 查 B 的单 → 404
- `test_create_ticket_for_other_order_404`：写入侧同样拦住，且 B 的工单列表仍为空

## 边界

- 演示数据是合成的，不接真实电商系统。
- 单实例 SQLite，没有连接池、没有分布式事务；并发上限就是 SQLite 的上限。
- 没有登录页和会话管理：token 由脚本签，真实系统里这一步属于登录服务。
- 没做限流、审计日志、工单幂等——这些排在后面几天。

## 接下来

| 阶段 | 内容 |
|---|---|
| 已完成 | 业务系统 + 令牌认证 + 越权拦截 |
| 下一步 | Agent 接工具：`customer_id` 由会话注入，模型只看得见单号 |
| 之后 | 知识库检索与拒答、工单幂等与审计、SSE 流式与并发、30 条评测集 |
