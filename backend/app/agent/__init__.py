"""Agent 能力包（M0：工具化改造）

本包为纯新增模块，不修改任何现有业务逻辑：

- `context`：请求级上下文（把 user / db 从工具参数中剔除，防越权）
- `tools`：工具注册表（把现有 service 能力包装为 LLM 可调用的工具）

设计文档：根目录《Agent改造设计方案.md》"改造 1：工具化改造"
"""
from app.agent.context import (  # noqa: F401
    AgentContext,
    agent_context,
    bind_context,
    current_context,
    get_context_or_raise,
    reset_context,
)
from app.agent.tools import (  # noqa: F401
    ADMIN_TOOLS,
    ALL_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    describe_tools,
    get_tools,
)

__all__ = [
    "AgentContext",
    "agent_context",
    "bind_context",
    "current_context",
    "get_context_or_raise",
    "reset_context",
    "ADMIN_TOOLS",
    "ALL_TOOLS",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "describe_tools",
    "get_tools",
]
