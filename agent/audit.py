"""工具调用审计：每次调用一行 JSONL。

为什么写文件和业务库分开
------------------------
审计是「事后追责」用的，写权限必须和业务数据分开。混在一张表里，等于让
被审计的一方持有审计日志的写权限——它出问题时第一件事就是改日志。

日志里**永远不写 token**，只写客户号。token 是凭证，泄露一次就得全量轮换；
客户号是标识，泄露了至少还能查出是谁、查过什么。

审计写失败不影响对话：磁盘满、权限错这类问题应该告警，但不该让用户
在这一句话上卡住。代价是可能丢一条记录——比丢掉整个会话便宜。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("desk.audit")

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("AUDIT_DIR", BASE_DIR / "logs"))


def record(
    *,
    session_id: str,
    customer_id: str,
    tool: str,
    args: dict[str, Any],
    result: str,
    ok: bool,
) -> None:
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": session_id,
        "customer": customer_id,
        "tool": tool,
        "args": args,
        "ok": ok,
        "result": result[:300],
    }
    path = LOG_DIR / f"audit-{datetime.now():%Y%m%d}.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("审计写入失败：%s", exc)


def tail(limit: int = 20) -> list[dict[str, Any]]:
    """读最近几条，给命令行和排查用。"""
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("audit-*.jsonl"), reverse=True)
    entries: list[dict[str, Any]] = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                return entries
    return entries
