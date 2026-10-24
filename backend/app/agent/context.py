"""Agent 请求级上下文：把 user / db 从「工具参数」中剔除

为什么不让 LLM 填 user_id（这是本模块存在的唯一理由）：
1. 安全：user_id 一旦暴露成工具参数，模型可能填出他人的 id，造成越权读写；
2. 技术：SQLAlchemy Session 无法序列化成 JSON Schema 交给模型。

做法：请求入口用 `bind_context()` / `agent_context()` 绑定，工具内部用
`current_context()` 读取。与 FastAPI 的 `Depends(get_current_user)` 思路一致，
只是作用域延伸到了「模型自主决定何时调用」的工具内部。

本模块不含任何业务逻辑，也不依赖具体 service，是纯基础设施。
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from app.core.roles import is_staff
from app.models import User

# 审计记录里参数/结果摘要的最大字符数，避免 trace 无界膨胀
TRACE_TEXT_LIMIT = 300


@dataclass
class AgentContext:
    """一次 Agent 运行期间共享的上下文（当前登录用户 + 数据库会话 + 调用轨迹）"""

    user: User
    db: Session
    source: str = "chat"  # 调用来源：chat | patrol | api | test
    trace: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def role(self) -> str:
        return getattr(self.user, "role", "user")

    @property
    def is_staff(self) -> bool:
        """管理员及以上（可访问全局数据与写类工具）"""
        return is_staff(self.user)

    def record(
        self,
        tool: str,
        args: Dict[str, Any],
        ok: bool,
        summary: str = "",
        elapsed_ms: int = 0,
    ) -> None:
        """记录一次工具调用，用于审计与前端轨迹展示"""
        self.trace.append(
            {
                "tool": tool,
                "args": {k: _clip(v) for k, v in (args or {}).items()},
                "ok": ok,
                "summary": _clip(summary, TRACE_TEXT_LIMIT),
                "ms": int(elapsed_ms),
                "at": time.time(),
            }
        )

    def tool_calls(self, tool: Optional[str] = None) -> int:
        """已发生的工具调用次数（可选按工具名过滤），供预算守卫使用"""
        if tool is None:
            return len(self.trace)
        return sum(1 for t in self.trace if t["tool"] == tool)

    def summary(self) -> Dict[str, Any]:
        """轨迹汇总（写库 / 前端展示用）"""
        return {
            "user_id": self.user_id,
            "source": self.source,
            "tool_calls": len(self.trace),
            "failed_calls": sum(1 for t in self.trace if not t["ok"]),
            "total_ms": sum(t["ms"] for t in self.trace),
            "steps": list(self.trace),
        }


def _clip(value: Any, limit: int = 80) -> Any:
    """截断过长的字符串值（保留可读的首部）"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(v, limit) for v in value][:10]
    if isinstance(value, dict):
        return {k: _clip(v, limit) for k, v in list(value.items())[:10]}
    return value


# 当前请求的上下文（每个请求/线程/任务各自一份，互不串扰）
_CURRENT: ContextVar[Optional[AgentContext]] = ContextVar("agent_context", default=None)


class ContextMissingError(RuntimeError):
    """工具被调用时没有绑定上下文——属于编程错误，正常链路上不会出现"""


def bind_context(ctx: AgentContext) -> Token:
    """绑定上下文，返回 token 供 reset 使用"""
    return _CURRENT.set(ctx)


def reset_context(token: Token) -> None:
    """还原到绑定前的上下文"""
    _CURRENT.reset(token)


def current_context() -> Optional[AgentContext]:
    """当前上下文；未绑定时返回 None（工具会优雅降级为错误提示，不抛异常）"""
    return _CURRENT.get()


def get_context_or_raise() -> AgentContext:
    """当前上下文；未绑定则抛异常（供必须要有上下文的调用方使用）"""
    ctx = _CURRENT.get()
    if ctx is None:
        raise ContextMissingError("Agent 上下文未绑定：请在请求入口使用 agent_context() 包裹")
    return ctx


@contextmanager
def agent_context(user: User, db: Session, source: str = "chat") -> Iterator[AgentContext]:
    """上下文管理器：进入时绑定，退出时自动还原

    用法：
        with agent_context(current_user, db) as ctx:
            result = knowledge_search.invoke({"query": "校招出差前准备"})
            print(ctx.summary())
    """
    ctx = AgentContext(user=user, db=db, source=source)
    token = bind_context(ctx)
    try:
        yield ctx
    finally:
        reset_context(token)
