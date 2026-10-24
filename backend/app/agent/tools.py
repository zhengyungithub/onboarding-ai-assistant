"""工具注册表（M0）：把现有 service 能力包装成 LLM 可调用的工具

四条硬性约定（后续 M1 的 Agent 图直接依赖这些约定）：

1. **只包装，不重写**——工具内部调用的仍是现有 service 函数，业务逻辑一行没动。
   检索算法、权限裁剪、向量同步全部沿用原实现，因此本模块上线不改变任何既有行为。
2. **永不抛异常**——工具返回 JSON 可序列化的 dict：成功 `{"ok": True, ...}`，
   失败 `{"ok": False, "error": "..."}`。异常被吞掉是刻意的：让模型看见失败原因
   并自行决定换关键词重试或换工具，而不是把整轮对话打断。
3. **权限在工具内统一校验**——写类工具要求管理员及以上；读类工具沿用 service 层
   的 `read_scope`，越权访问只会得到「不存在或无权访问」，不泄露是否存在。
4. **每次调用写入 `AgentContext.trace`**——审计与前端轨迹展示的数据来源。

设计文档：根目录《Agent改造设计方案.md》"改造 1：工具化改造"
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from app.agent.context import AgentContext, current_context
from app.core.roles import is_staff
from app.models import User
from app.schemas.knowledge import KnowledgeItemCreate, KnowledgeItemUpdate
from app.schemas.memo import MemoCreate
from app.services import (
    chat_service,
    evaluation_service,
    flow_service,
    knowledge_service,
    memo_service,
)

# 单条引用/条目内容回传给模型时的最大字符数（控制上下文长度）
MAX_SNIPPET = 800
# 备忘录、流程节点等列表类结果的默认条数
DEFAULT_LIST_LIMIT = 20


# ---------------------------------------------------------------------------
# 统一包装：上下文 + 权限 + 计时 + 异常兜底 + 审计
# ---------------------------------------------------------------------------

def _brief(result: Any, limit: int = 200) -> str:
    """从工具返回值里抽一句摘要，写进 trace（避免 trace 里塞满原文）"""
    if isinstance(result, dict):
        for key in ("error", "message", "summary"):
            if result.get(key):
                return str(result[key])[:limit]
        if "count" in result:
            return f"count={result['count']}"
        return ",".join(list(result.keys())[:6])
    return str(result)[:limit]


def _audited(name: str, staff_only: bool = False) -> Callable:
    """工具统一包装器

    staff_only=True 的工具对普通用户直接返回权限错误（不执行、不泄露数据）。
    """

    def decorator(fn: Callable) -> Callable:
        def wrapper(**kwargs) -> Dict[str, Any]:
            ctx: Optional[AgentContext] = current_context()
            if ctx is None:
                # 编程错误场景：优雅降级而不是抛异常，方便定位
                return {
                    "ok": False,
                    "error": "Agent 上下文未绑定，工具不可用（请检查请求入口是否正确绑定上下文）",
                }
            if staff_only and not ctx.is_staff:
                result: Dict[str, Any] = {
                    "ok": False,
                    "error": f"权限不足：{name} 仅管理员及以上可用",
                }
                ctx.record(name, kwargs, False, result["error"], 0)
                return result

            start = time.perf_counter()
            try:
                result = fn(ctx, **kwargs)
                # 业务失败（返回 ok=False，如「条目不存在」「权限不足」）也计为失败，
                # 否则轨迹里会把失败调用记成成功，影响后续质量度量与调优
                ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
            except Exception as exc:  # noqa: BLE001 —— 刻意兜住所有异常
                ok = False
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            ctx.record(name, kwargs, ok, _brief(result), elapsed_ms)
            return result

        wrapper.__name__ = name
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


def _ref_brief(ref: Dict[str, Any], snippet_limit: int = MAX_SNIPPET) -> Dict[str, Any]:
    """把检索结果裁剪成回传给模型的紧凑结构"""
    content = ref.get("content") or ""
    if len(content) > snippet_limit:
        content = content[:snippet_limit] + "…"
    return {
        "id": ref.get("id"),
        "title": ref.get("title", ""),
        "source_type": ref.get("source_type", "knowledge_item"),
        "score": round(float(ref.get("score") or 0), 4),
        "content": content,
    }


# ---------------------------------------------------------------------------
# 读工具（所有用户可用）
# ---------------------------------------------------------------------------

class KnowledgeSearchInput(BaseModel):
    query: str = Field(
        ...,
        description=(
            "检索用的自然语言问题，必须自包含、不依赖对话上下文"
            "（例如「校招出差前需要做哪些准备」，而不是「还有呢」）"
        ),
    )
    top_k: int = Field(default=8, ge=1, le=15, description="返回条数，默认 8，最大 15")


@tool("knowledge_search", args_schema=KnowledgeSearchInput)
@_audited("knowledge_search")
def knowledge_search(ctx: AgentContext, query: str, top_k: int = 8) -> Dict[str, Any]:
    """检索知识库，返回结构化知识条目、流程节点与已入库文档片段（两阶段混合检索）。

    这是最常用的工具：任何与新人入职业务操作相关的问题都优先调用它。
    如果返回结果与问题不相关或明显不足，可以换用更具体的关键词
    （例如换成流程名、系统名、物料名）再调用一次。
    """
    refs = chat_service.search_relevant_items(ctx.user, query, db=ctx.db, limit=top_k)
    return {
        "ok": True,
        "count": len(refs),
        "results": [_ref_brief(r) for r in refs],
        "message": "无相关结果" if not refs else f"命中 {len(refs)} 条",
    }


class DocumentSearchInput(BaseModel):
    query: str = Field(..., description="检索用的关键词或问题，用于在已上传的原始文档中定位片段")
    max_results: int = Field(default=3, ge=1, le=5, description="最多返回的文档数，默认 3")


@tool("document_search", args_schema=DocumentSearchInput)
@_audited("document_search")
def document_search(ctx: AgentContext, query: str, max_results: int = 3) -> Dict[str, Any]:
    """在「已上传的原始文档」（Word/Excel/PDF 解析后的文本）中按关键词查找相关片段。

    适用场景：需要给出原始文档出处时；knowledge_search 结果不足、想再翻一遍原文时。
    注意：该工具过滤较严（优先精确关键词命中），查不到属正常，不代表知识库没有。
    """
    docs = chat_service._search_documents(ctx.user, query, ctx.db)[:max_results]
    return {
        "ok": True,
        "count": len(docs),
        "results": [_ref_brief(d) for d in docs],
        "message": "无相关文档片段" if not docs else f"命中 {len(docs)} 篇文档",
    }


class ListFlowsInput(BaseModel):
    keyword: Optional[str] = Field(
        default=None, description="按流程名称过滤的关键词，例如「校招」；不传则返回全部流程"
    )


@tool("list_flows", args_schema=ListFlowsInput)
@_audited("list_flows")
def list_flows(ctx: AgentContext, keyword: Optional[str] = None) -> Dict[str, Any]:
    """列出业务流程及其节点（含每个节点的 id）。

    何时使用：用户问「有哪些流程」；或需要 flow_next_step 时先拿到 flow_id 与 node_id。
    """
    _total, flows = flow_service.get_flows(ctx.db, ctx.user, skip=0, limit=50)
    if keyword:
        flows = [f for f in flows if keyword in (f.name or "")]
    results = []
    for flow in flows:
        nodes = sorted(flow.nodes, key=lambda n: (n.order or 0))
        results.append(
            {
                "flow_id": flow.id,
                "name": flow.name,
                "description": flow.description or "",
                "node_count": len(nodes),
                "nodes": [
                    {"node_id": n.id, "name": n.name, "order": n.order or 0}
                    for n in nodes
                ],
            }
        )
    return {
        "ok": True,
        "count": len(results),
        "flows": results,
        "message": "无流程" if not results else f"{len(results)} 条流程",
    }


class FlowNextStepInput(BaseModel):
    flow_id: str = Field(..., description="流程 ID，需先通过 list_flows 获取")
    current_node_id: str = Field(
        default="",
        description="当前所在节点的 node_id；留空表示「还没开始」，此时返回流程第一步",
    )


@tool("flow_next_step", args_schema=FlowNextStepInput)
@_audited("flow_next_step")
def flow_next_step(ctx: AgentContext, flow_id: str, current_node_id: str = "") -> Dict[str, Any]:
    """查询某个流程「当前节点做完之后，下一步是什么」，并带上该步骤关联的知识条目。

    何时使用：用户说「我现在做完了 X，接下来做什么」「这个流程第一步是什么」。
    若 current_node_id 留空，返回流程的第一步（起点）。
    """
    flow = flow_service.get_flow(ctx.db, flow_id, ctx.user)
    if flow is None:
        return {"ok": False, "error": "流程不存在或无权访问"}

    nodes = sorted(flow.nodes, key=lambda n: (n.order or 0))
    if not nodes:
        return {"ok": False, "error": f"流程「{flow.name}」还没有配置任何节点"}

    if not current_node_id:
        first = nodes[0]
        return {
            "ok": True,
            "flow_name": flow.name,
            "position": "start",
            "next_node": {
                "node_id": first.id,
                "name": first.name,
                "description": first.description or "",
                "order": first.order or 0,
            },
            "related_items": flow_service.get_related_items(ctx.db, first),
            "message": f"流程「{flow.name}」的第一步",
        }

    # 校验当前节点属于该流程，避免传错 id 时静默返回错误结果
    if not any(n.id == current_node_id for n in nodes):
        return {"ok": False, "error": "当前节点不属于该流程，请先用 list_flows 核对 node_id"}

    next_node = flow_service.get_next_node(ctx.db, flow_id, current_node_id, ctx.user)
    if next_node is None:
        return {
            "ok": True,
            "flow_name": flow.name,
            "position": "end",
            "finished": True,
            "next_node": None,
            "related_items": [],
            "message": f"流程「{flow.name}」已完成，当前步骤是最后一步",
        }

    return {
        "ok": True,
        "flow_name": flow.name,
        "position": "middle",
        "next_node": {
            "node_id": next_node.id,
            "name": next_node.name,
            "description": next_node.description or "",
            "order": next_node.order or 0,
        },
        "related_items": flow_service.get_related_items(ctx.db, next_node),
        "message": f"下一步：{next_node.name}",
    }


class GetItemInput(BaseModel):
    item_id: str = Field(..., description="知识条目 ID（来自 knowledge_search 结果里的 id）")


@tool("get_item", args_schema=GetItemInput)
@_audited("get_item")
def get_item(ctx: AgentContext, item_id: str) -> Dict[str, Any]:
    """按 ID 读取单条知识条目的完整内容（标题、步骤、涉及系统/物料、注意事项）。

    何时使用：knowledge_search 只回传了片段、需要完整步骤时。
    """
    item = knowledge_service.get_item(ctx.db, item_id, ctx.user)
    if item is None:
        return {"ok": False, "error": "条目不存在或无权访问"}
    return {
        "ok": True,
        "item": {
            "id": item.id,
            "title": item.title,
            "status": item.status,
            "steps": item.steps or [],
            "system_or_material": item.system_or_material or "",
            "caution": item.caution or "",
            "related_flow": item.related_flow or [],
        },
        "message": f"已读取条目「{item.title}」",
    }


class MemoListInput(BaseModel):
    keyword: Optional[str] = Field(default=None, description="按标题关键词过滤")
    folder_id: Optional[str] = Field(default=None, description="只看某个文件夹（含子文件夹）")
    only_pending: bool = Field(default=True, description="只列未完成的待办，默认 True")


@tool("memo_list", args_schema=MemoListInput)
@_audited("memo_list")
def memo_list(
    ctx: AgentContext,
    keyword: Optional[str] = None,
    folder_id: Optional[str] = None,
    only_pending: bool = True,
) -> Dict[str, Any]:
    """查看当前用户自己的待办事项（备忘录）。

    何时使用：用户问「我有哪些待办」「还有什么没做完」。
    注意：只能看到用户自己的备忘录，与其他用户互相隔离。
    """
    _total, memos = memo_service.get_memos(
        ctx.db,
        ctx.user_id,
        is_completed=False if only_pending else None,
        keyword=keyword,
        folder_id=folder_id,
        skip=0,
        limit=DEFAULT_LIST_LIMIT,
    )
    return {
        "ok": True,
        "count": len(memos),
        "memos": [
            {
                "id": m.id,
                "title": m.title,
                "priority": m.priority,
                "is_completed": m.is_completed,
                "due_date": m.due_date.isoformat() if m.due_date else None,
                "folder_id": m.folder_id,
            }
            for m in memos
        ],
        "message": "暂无待办" if not memos else f"{len(memos)} 条待办",
    }


# ---------------------------------------------------------------------------
# 写工具
# ---------------------------------------------------------------------------

class MemoCreateInput(BaseModel):
    title: str = Field(..., max_length=200, description="待办标题，简洁一句话")
    content: Optional[str] = Field(default=None, description="补充说明，可省略")
    priority: str = Field(default="medium", description="优先级：high / medium / low")
    due_date: Optional[str] = Field(
        default=None,
        description="截止时间，ISO 格式如 2026-10-15T09:00:00，可省略",
    )
    folder_id: Optional[str] = Field(default=None, description="归入的文件夹 ID，可省略")


@tool("memo_create", args_schema=MemoCreateInput)
@_audited("memo_create")
def memo_create(
    ctx: AgentContext,
    title: str,
    content: Optional[str] = None,
    priority: str = "medium",
    due_date: Optional[str] = None,
    folder_id: Optional[str] = None,
) -> Dict[str, Any]:
    """为当前用户新建一条待办事项（备忘录）。

    何时使用：用户说「帮我记一下」「提醒我明天做 X」。
    这是写操作：写入前应先向用户复述将要创建的内容（标题、截止时间）并得到确认。
    """
    memo = memo_service.create_memo(
        ctx.db,
        ctx.user,
        MemoCreate(
            title=title,
            content=content,
            priority=priority or "medium",
            due_date=due_date,
            folder_id=folder_id,
        ),
    )
    return {
        "ok": True,
        "memo_id": memo.id,
        "title": memo.title,
        "priority": memo.priority,
        "folder_id": memo.folder_id,
        "message": f"已创建待办「{memo.title}」",
    }


class CreateDraftItemInput(BaseModel):
    title: str = Field(..., max_length=200, description="操作名称，例如「格慧招候选人信息录入」")
    steps: Optional[List[str]] = Field(default=None, description="操作步骤数组，按顺序排列")
    system_or_material: Optional[str] = Field(default=None, description="涉及的系统或物料")
    caution: Optional[str] = Field(default=None, description="注意事项")
    related_flow: Optional[List[str]] = Field(default=None, description="关联的流程 ID 列表")


@tool("create_draft_item", args_schema=CreateDraftItemInput)
@_audited("create_draft_item", staff_only=True)
def create_draft_item(
    ctx: AgentContext,
    title: str,
    steps: Optional[List[str]] = None,
    system_or_material: Optional[str] = None,
    caution: Optional[str] = None,
    related_flow: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """把整理好的内容新建为一条**草稿**知识条目（管理员及以上）。

    硬约束：只能创建 draft 状态。草稿不会进入检索、不会被问答引用，
    必须由人在「知识库管理」页面确认后才发布——避免模型把不确定的内容直接变成"官方知识"。
    """
    item = knowledge_service.create_item(
        ctx.db,
        ctx.user,
        KnowledgeItemCreate(
            title=title,
            steps=steps,
            system_or_material=system_or_material,
            caution=caution,
            related_flow=related_flow,
            status="draft",
        ),
    )
    return {
        "ok": True,
        "item_id": item.id,
        "title": item.title,
        "status": item.status,
        "message": f"已创建草稿条目「{item.title}」，需在知识库页面人工确认后发布",
    }


class UpdateItemInput(BaseModel):
    item_id: str = Field(..., description="要修改的知识条目 ID")
    title: Optional[str] = Field(default=None, description="新的标题")
    steps: Optional[List[str]] = Field(default=None, description="新的步骤数组")
    system_or_material: Optional[str] = Field(default=None, description="新的涉及系统/物料")
    caution: Optional[str] = Field(default=None, description="新的注意事项")
    related_flow: Optional[List[str]] = Field(default=None, description="新的关联流程 ID 列表")


@tool("update_item", args_schema=UpdateItemInput)
@_audited("update_item", staff_only=True)
def update_item(
    ctx: AgentContext,
    item_id: str,
    title: Optional[str] = None,
    steps: Optional[List[str]] = None,
    system_or_material: Optional[str] = None,
    caution: Optional[str] = None,
    related_flow: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """修改已有知识条目的内容（管理员及以上）。

    注意：**不提供发布/下架能力**（status 不在可改字段内）。
    状态变更属于人工决策，请在「知识库管理」页面操作。
    修改前应先向用户复述将要改动的内容并得到确认。
    """
    patch = {
        k: v
        for k, v in {
            "title": title,
            "steps": steps,
            "system_or_material": system_or_material,
            "caution": caution,
            "related_flow": related_flow,
        }.items()
        if v is not None
    }
    if not patch:
        return {"ok": False, "error": "没有提供任何要修改的字段"}

    item = knowledge_service.update_item(
        ctx.db, item_id, ctx.user, KnowledgeItemUpdate(**patch)
    )
    if item is None:
        return {"ok": False, "error": "条目不存在或无权修改"}
    return {
        "ok": True,
        "item_id": item.id,
        "title": item.title,
        "status": item.status,
        "changed": sorted(patch.keys()),
        "message": f"已更新条目「{item.title}」",
    }


# ---------------------------------------------------------------------------
# 管理工具
# ---------------------------------------------------------------------------

class StatsSnapshotInput(BaseModel):
    pass


@tool("stats_snapshot", args_schema=StatsSnapshotInput)
@_audited("stats_snapshot", staff_only=True)
def stats_snapshot(ctx: AgentContext) -> Dict[str, Any]:
    """查看知识库与问答质量的整体统计（管理员及以上）。

    返回：条目总数、已发布数、测试用例数、最近一次评测命中率、有用/没用反馈数。
    何时使用：用户问「知识库现在什么情况」「效果怎么样」。
    """
    stats = evaluation_service.get_stats(ctx.db, ctx.user_id)
    return {"ok": True, "stats": stats, "message": "已获取统计快照"}


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

READ_TOOLS: List[BaseTool] = [
    knowledge_search,
    document_search,
    list_flows,
    flow_next_step,
    get_item,
    memo_list,
]

# 写工具：memo_create 写入的是用户自己的数据；后两个在工具内部校验管理员权限
WRITE_TOOLS: List[BaseTool] = [
    memo_create,
    create_draft_item,
    update_item,
]

ADMIN_TOOLS: List[BaseTool] = [
    stats_snapshot,
]

ALL_TOOLS: List[BaseTool] = READ_TOOLS + WRITE_TOOLS + ADMIN_TOOLS

# 名称索引，便于按名取工具与断言
TOOLS_BY_NAME: Dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}


def get_tools(
    user: Optional[User] = None,
    include_write: bool = True,
    include_admin: bool = True,
) -> List[BaseTool]:
    """按用户角色与用途返回可用工具集（M1 的 Agent 图用这个函数取工具）

    - 读工具：所有用户
    - 写工具：include_write=True 时纳入（工具内部再校验是否为管理员）
    - 管理工具：仅管理员及以上（user=None 视为内部/系统调用，全部返回）
    """
    tools: List[BaseTool] = list(READ_TOOLS)
    if include_write:
        tools += WRITE_TOOLS
    if include_admin and (user is None or is_staff(user)):
        tools += ADMIN_TOOLS
    return tools


def describe_tools(user: Optional[User] = None) -> List[Dict[str, Any]]:
    """工具清单（名称 + 描述 + 参数 schema），用于调试、文档与测试"""
    described = []
    for t in get_tools(user):
        schema = t.args_schema.model_json_schema() if t.args_schema else {}
        described.append(
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "parameters": sorted((schema.get("properties") or {}).keys()),
            }
        )
    return described
