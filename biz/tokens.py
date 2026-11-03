"""客户身份令牌：只承载「你是谁」，不承载任何业务数据。

为什么不用 JWT
--------------
这里要证明的不是「我会引一个库」，是「我知道身份在链路的哪一环被确定」。
一个 HMAC 签名的短 token 足够说明问题，少一个依赖，也少一处
「算法选错、密钥混进 payload」的坑。

密钥从环境变量读，代码里给的是开发用默认值——生产上不换就是事故，
所以启动时会打一条警告（见 app.py）。
"""

import hashlib
import hmac
import os

DEFAULT_SECRET = "dev-secret-change-me"
SIG_LEN = 16


def _secret() -> bytes:
    """每次调用都读环境变量，不做模块级缓存。

    模块级缓存会变成「谁先 import 谁决定配置」——dotenv 晚一步加载就被吃掉，
    这类 bug 在本地跑不出来、换个启动方式就变，排查成本极高。
    多读一次 os.environ 的代价，比这个坑便宜得多。
    """
    return os.environ.get("DESK_SECRET", DEFAULT_SECRET).encode()


def using_default_secret() -> bool:
    return os.environ.get("DESK_SECRET", DEFAULT_SECRET) == DEFAULT_SECRET


def issue(customer_id: str) -> str:
    """签发 token：`客户号.签名前 16 位`。

    只在本地开发与测试里用；真实系统由登录服务签发，
    业务系统只负责验，不负责发。
    """
    sig = hmac.new(_secret(), customer_id.encode(), hashlib.sha256).hexdigest()[:SIG_LEN]
    return f"{customer_id}.{sig}"


def verify(token: str) -> str | None:
    """校验通过返回 customer_id，否则返回 None。

    用 `hmac.compare_digest` 而不是 `==`：后者按字节短路比较，理论上能靠
    响应时间差猜出签名（时序攻击）。演示项目里这点差别不影响功能，但它是
    「知道自己在写认证」和「抄了一段能跑的代码」的分界线。
    """
    customer_id, _, sig = token.rpartition(".")
    if not customer_id or not sig:
        return None
    expect = hmac.new(_secret(), customer_id.encode(), hashlib.sha256).hexdigest()[:SIG_LEN]
    return customer_id if hmac.compare_digest(sig, expect) else None
