"""跑评测集：30 条真实对话，用真模型。

判三件事，都只看确定性证据：

1. **工具选对了没**：期望的工具至少命中一个，禁止的工具一个都不能出现；
2. **答案里该有的有没有**：`must_match` 的正则全部要匹配；
3. **不该有的有没有**：`must_not_match` 任一匹配就算失败（比如别人的单号、
   编造的天数）。

不打「答案好不好」这种分：那需要另一个模型来评，脚本就变成「模型 A 评价
模型 B」，出了问题说不清是谁的错。宁可判得粗，也要判得能复现。

    python -m scripts.eval_agent
    python -m scripts.eval_agent --group 越权
    python -m scripts.eval_agent --limit 5 --verbose
"""

import argparse
import json
import re
import sys
from pathlib import Path

from agent.biz_client import BizClient
from agent.config import BIZ_BASE_URL
from agent.loop import run_turn
from agent.session import Session
from biz import tokens

CASES_PATH = Path(__file__).resolve().parent.parent / "eval" / "cases.json"


def judge(case: dict, tools: set[str], reply: str) -> list[str]:
    problems: list[str] = []

    expected = set(case.get("expect_tools") or [])
    if expected and not (tools & expected):
        problems.append(f"没调期望的工具（实际：{sorted(tools) or '无'}）")

    forbidden = tools & set(case.get("forbid_tools") or [])
    if forbidden:
        problems.append(f"调了不该调的工具：{sorted(forbidden)}")

    for pattern in case.get("must_match") or []:
        if not re.search(pattern, reply):
            problems.append(f"缺 /{pattern}/")

    for pattern in case.get("must_not_match") or []:
        if re.search(pattern, reply):
            problems.append(f"出现禁忌 /{pattern}/")

    return problems


def run_case(case: dict) -> tuple[list[str], str, list[str]]:
    token = tokens.issue(case["as"])
    session = Session(id=f"eval-{case['id']}", token=token, customer_id=case["as"])
    client = BizClient(BIZ_BASE_URL, token)
    tools: list[str] = []
    reply = ""
    try:
        for text in case["turns"]:
            turn = run_turn(session, text, client)
            tools.extend(step.tool for step in turn.steps)
            reply = turn.reply
    except Exception as exc:  # noqa: BLE001 - 评测脚本要跑完全部用例
        return [f"异常：{exc.__class__.__name__}: {exc}"], reply, tools
    finally:
        client.close()
    return judge(case, set(tools), reply), reply, tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Desk Agent 评测")
    parser.add_argument("--group", help="只跑某一组")
    parser.add_argument("--limit", type=int, help="只跑前 N 条")
    parser.add_argument("--verbose", action="store_true", help="打印每条的回答")
    args = parser.parse_args(argv)

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.group:
        cases = [c for c in cases if c["group"] == args.group]
    if args.limit:
        cases = cases[: args.limit]

    if not cases:
        print("没有匹配的用例")
        return 1

    print(f"共 {len(cases)} 条，模型调用中…\n")
    passed = 0
    by_group: dict[str, list[int]] = {}

    for case in cases:
        problems, reply, tools = run_case(case)
        ok = not problems
        passed += ok
        bucket = by_group.setdefault(case["group"], [0, 0])
        bucket[0] += ok
        bucket[1] += 1

        mark = "ok  " if ok else "FAIL"
        first = case["turns"][0][:26]
        print(f"[{mark}] {case['id']:<3} {case['group']:<4} {first}")
        if not ok:
            for problem in problems:
                print(f"        - {problem}")
            print(f"        工具：{tools or '无'}")
            print(f"        回答：{reply[:160].replace(chr(10), ' ')}")
        elif args.verbose:
            print(f"        工具：{tools or '无'}")
            print(f"        回答：{reply[:160].replace(chr(10), ' ')}")

    print("\n" + "-" * 56)
    for group, (ok, total) in by_group.items():
        print(f"{group:<6} {ok}/{total}  {ok / total:.0%}")
    print("-" * 56)
    print(f"合计   {passed}/{len(cases)}  {passed / len(cases):.0%}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
