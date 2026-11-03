"""命令行里跑一轮对话，看得到工具调用轨迹。

    python -m scripts.chat "我上周买的键盘到哪了"
    python -m scripts.chat --as C1002 "帮我查 SF20261028001"

`--as` 是给越权演示用的：换一个客户身份去查别人的单，会得到「订单不存在」。
两个身份都跑一遍，比嘴上说「拦得住」有说服力。
"""

import argparse
import sys

from agent.biz_client import BizClient, BizError
from agent.config import BIZ_BASE_URL
from agent.loop import run_turn
from agent.session import Session
from biz import tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desk Agent 命令行")
    parser.add_argument("message", help="用户这句话")
    parser.add_argument("--as", dest="customer", default="C1001", help="以哪个客户身份发问")
    parser.add_argument("--show-context", action="store_true", help="打印最终上下文条数")
    args = parser.parse_args(argv)

    token = tokens.issue(args.customer)
    session = Session(id="cli", token=token)

    print(f"[身份] {args.customer}  [问题] {args.message}\n")
    client = BizClient(BIZ_BASE_URL, token)
    try:
        turn = run_turn(session, args.message, client)
    except BizError as exc:
        print(f"业务系统错误：{exc.message}")
        return 2
    finally:
        client.close()

    for i, step in enumerate(turn.steps, 1):
        preview = step.result.replace("\n", " / ")[:110]
        print(f"  [{i}] {step.tool}({step.args}) -> {preview}")
    if turn.steps:
        print()

    print(f"回答：{turn.reply}")
    print(f"\n[轮次 {turn.rounds}{'，已截断' if turn.truncated else ''}]", end="")
    if args.show_context:
        print(f"  [上下文 {len(session.messages)} 条]")
    else:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
