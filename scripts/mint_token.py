"""签发本地开发用的 token。

    python -m scripts.mint_token C1001
    python -m scripts.mint_token C1001 C1002

一次给两个号是刻意的：做越权测试时手上有 A 和 B 两个令牌，
才能证明「A 拿自己的 token 查 B 的单会 404」。

生产环境不存在这个脚本——token 由登录服务签发，
业务系统只负责验。
"""

import sys

from biz import tokens


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip())
        return 1
    for customer_id in argv:
        print(f"{customer_id}\t{tokens.issue(customer_id)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
