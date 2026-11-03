"""会话状态。

两条规矩，都写在这里而不是散在调用链上：

1. **token 只存在 Session 里，永远不进 messages。**
   常见反例是把「你是张伟，客户号 C1001」拼进 system prompt。模型确实会
   照着说，但它同时也知道了客户号——之后任何一次提示注入、越狱、或者只是
   模型自己串了上下文，都可能把它写进工具参数或回复里。
   身份不需要模型知道，它只需要「能查自己的」这个能力。

2. **messages 只放模型该看的东西**：用户说的话、助手的回复、工具返回的结果。
   不塞 token、不塞数据库、不塞别人的数据。
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

TTL_SECONDS = 3600


@dataclass
class Session:
    id: str
    token: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turns: int = 0

    def touch(self) -> None:
        self.updated_at = time.time()
        self.turns += 1


class SessionStore:
    """内存会话表。

    单进程、无持久化——这是有意的边界：多副本部署时这张表必须换成 Redis，
    而 token 的存放位置也随之变化（见 README 的「边界」一节）。
    """

    def __init__(self, ttl: int = TTL_SECONDS) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl
        self._lock = threading.Lock()

    def create(self, token: str) -> Session:
        with self._lock:
            self._purge_locked()
            session = Session(id=uuid.uuid4().hex[:12], token=token)
            self._sessions[session.id] = session
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def _purge_locked(self) -> None:
        now = time.time()
        expired = [k for k, s in self._sessions.items() if now - s.updated_at > self._ttl]
        for key in expired:
            del self._sessions[key]

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)


store = SessionStore()
