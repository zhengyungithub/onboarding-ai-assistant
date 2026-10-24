"""Agent 工具注册表回归（M0 工具化改造）

守护点：
1. 注册表完备：10 个工具按 role 正确暴露，schema 完整（管理员工具不对普通用户开放）；
2. 上下文强制：未绑定上下文时优雅返回错误，绝不抛异常打断对话；
3. 只包装不重写：工具结果与底层 service 一致，裁剪/脱敏符合约定；
4. 异常兜底：底层 service 抛错 → 工具返回 ok=False（让模型自己决策），不冒泡；
5. 权限边界：普通用户拿不到管理员写工具；越权读条目只得到「不存在或无权访问」；
6. 写操作约束：create_draft_item 只能建 draft（不可绕过人工发布）；update_item 无 status 字段；
7. 数据隔离：memo_list 只能看到自己的备忘录；审计轨迹完整。

全程离线：LLM / Embedding / Qdrant 一律通过 monkeypatch 规避。
"""
import pytest

from tests.helpers import expect, body, hdr, make_item, make_flow, add_node, user_obj

from app.agent import (
    ADMIN_TOOLS,
    ALL_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    describe_tools,
    get_tools,
)
from app.agent.context import agent_context, current_context
from app.agent.tools import TOOLS_BY_NAME


def _tool(name: str):
    return TOOLS_BY_NAME[name]


def _call(name: str, client_ctx, args: dict):
    """在绑定上下文的条件下调用工具（模拟 Agent 调用路径）"""
    with agent_context(client_ctx["user"], client_ctx["db"], source="test") as actx:
        result = _tool(name).invoke(args)
        trace = list(actx.trace)
    return result, trace


def _actor(db, username):
    return {"user": user_obj(db, username), "db": db}


# ===== 1. 注册表与 schema =====

def test_registry_shape(db, ctx):
    """工具数量、名称与分组符合设计（10 个：6 读 + 3 写 + 1 管理）"""
    assert len(READ_TOOLS) == 6
    assert len(WRITE_TOOLS) == 3
    assert len(ADMIN_TOOLS) == 1
    assert len(ALL_TOOLS) == 10

    names = {t.name for t in ALL_TOOLS}
    assert names == {
        "knowledge_search",
        "document_search",
        "list_flows",
        "flow_next_step",
        "get_item",
        "memo_list",
        "memo_create",
        "create_draft_item",
        "update_item",
        "stats_snapshot",
    }

    # 每个工具都要有描述与参数 schema（模型靠这两样决定怎么调）
    for t in ALL_TOOLS:
        assert (t.description or "").strip(), f"工具 {t.name} 缺少描述"
        assert t.args_schema is not None, f"工具 {t.name} 缺少参数 schema"


def test_get_tools_filters_by_role(db, ctx):
    """管理员工具不给普通用户；user=None 视为系统调用返回全部"""
    normal = user_obj(db, f"regr_ua")
    admin = user_obj(db, f"regr_admin")

    normal_names = {t.name for t in get_tools(user=normal)}
    admin_names = {t.name for t in get_tools(user=admin)}
    system_names = {t.name for t in get_tools()}

    assert "stats_snapshot" not in normal_names
    assert "stats_snapshot" in admin_names
    assert len(system_names) == 10
    # 读工具对所有人开放
    for t in READ_TOOLS:
        assert t.name in normal_names


def test_describe_tools_exposes_parameters(db, ctx):
    """describe_tools 能给出参数名，便于文档与调试"""
    described = {d["name"]: d for d in describe_tools(user_obj(db, "regr_admin"))}
    assert described["knowledge_search"]["parameters"] == ["query", "top_k"]
    assert "flow_id" in described["flow_next_step"]["parameters"]
    # 发布能力必须不在参数里（人工决策，模型不可代劳）
    assert "status" not in described["update_item"]["parameters"]


def test_args_schema_rejects_invalid_args(db, ctx):
    """参数越界由 schema 拦住（top_k 上限 15），不会打到 service 层"""
    actor = _actor(db, "regr_ua")
    with agent_context(actor["user"], actor["db"], source="test"):
        with pytest.raises(Exception):
            _tool("knowledge_search").invoke({"query": "校招", "top_k": 99})
        with pytest.raises(Exception):
            _tool("knowledge_search").invoke({"top_k": 3})  # 缺必填 query


# ===== 2. 上下文强制 =====

def test_tool_without_context_returns_error(db, ctx):
    """未绑定上下文时返回结构化错误，不抛异常"""
    assert current_context() is None
    result = _tool("list_flows").invoke({})
    assert result["ok"] is False
    assert "上下文未绑定" in result["error"]


# ===== 3. 读工具包装正确性 =====

def test_knowledge_search_wraps_service(db, ctx, monkeypatch):
    """knowledge_search 透传 service 结果并裁剪 content 长度"""
    long_text = "步骤内容" * 500
    fake = [
        {
            "id": "item-1",
            "title": "校招出差前准备",
            "content": long_text,
            "source_type": "knowledge_item",
            "score": 0.81234,
        }
    ]
    captured = {}

    def fake_search(user, question, db=None, limit=8):
        captured.update({"question": question, "limit": limit, "user_id": user.id})
        return fake

    monkeypatch.setattr("app.services.chat_service.search_relevant_items", fake_search)

    actor = _actor(db, "regr_ua")
    result, trace = _call(
        "knowledge_search", actor, {"query": "校招出差前要做什么", "top_k": 5}
    )

    assert result["ok"] is True
    assert result["count"] == 1
    ref = result["results"][0]
    assert ref["id"] == "item-1"
    assert ref["source_type"] == "knowledge_item"
    assert ref["score"] == 0.8123
    assert len(ref["content"]) <= 801  # MAX_SNIPPET + 省略号
    # 参数原样透传给 service，没有被工具改写
    assert captured == {"question": "校招出差前要做什么", "limit": 5, "user_id": actor["user"].id}
    # 审计轨迹
    assert len(trace) == 1 and trace[0]["tool"] == "knowledge_search" and trace[0]["ok"] is True


def test_document_search_wraps_service(db, ctx, monkeypatch):
    """document_search 复用严格过滤的文档检索，并限制返回条数"""
    fake = [
        {"id": f"doc-{i}", "title": f"资料{i}.docx", "content": "片段", "source_type": "document", "score": 0.5}
        for i in range(5)
    ]
    monkeypatch.setattr("app.services.chat_service._search_documents", lambda u, q, d: fake)

    actor = _actor(db, "regr_ua")
    result, _ = _call("document_search", actor, {"query": "物料邮寄", "max_results": 2})
    assert result["ok"] is True
    assert result["count"] == 2
    assert [r["source_type"] for r in result["results"]] == ["document", "document"]


def test_knowledge_search_empty_returns_ok(db, ctx, monkeypatch):
    """查不到不是错误：ok=True + count=0，让模型据此决定下一步（换词或说不知道）"""
    monkeypatch.setattr("app.services.chat_service.search_relevant_items", lambda *a, **k: [])
    actor = _actor(db, "regr_ua")
    result, _ = _call("knowledge_search", actor, {"query": "完全不相关的问题"})
    assert result["ok"] is True and result["count"] == 0
    assert result["message"] == "无相关结果"


def test_list_flows_and_next_step(client, db, ctx):
    """list_flows 给出节点 id；flow_next_step 支持起点/中间/终点三种位置"""
    token = ctx["admin"]["token"]
    flow = make_flow(client, token, "工具测试流程-校招")
    n1 = add_node(client, token, flow["id"], "简历初筛", 1, "收集并初筛简历")
    n2 = add_node(client, token, flow["id"], "面试安排", 2, "约面试官时间")
    add_node(client, token, flow["id"], "发 offer", 3, "审批后发出")

    actor = _actor(db, f"regr_admin")

    result, _ = _call("list_flows", actor, {"keyword": "工具测试流程"})
    assert result["ok"] is True and result["count"] == 1
    nodes = result["flows"][0]["nodes"]
    assert [n["name"] for n in nodes] == ["简历初筛", "面试安排", "发 offer"]
    assert result["flows"][0]["node_count"] == 3

    # 起点：current_node_id 留空
    start, _ = _call("flow_next_step", actor, {"flow_id": flow["id"]})
    assert start["ok"] is True and start["position"] == "start"
    assert start["next_node"]["node_id"] == n1["id"]

    # 中间：返回下一节点及其说明
    mid, _ = _call("flow_next_step", actor, {"flow_id": flow["id"], "current_node_id": n1["id"]})
    assert mid["position"] == "middle"
    assert mid["next_node"]["node_id"] == n2["id"]
    assert mid["next_node"]["description"] == "约面试官时间"

    # 终点：明确告知已完成
    last_node = nodes[-1]
    end, _ = _call(
        "flow_next_step", actor, {"flow_id": flow["id"], "current_node_id": last_node["node_id"]}
    )
    assert end["ok"] is True and end["finished"] is True and end["position"] == "end"


def test_flow_next_step_rejects_foreign_node(client, db, ctx):
    """传入不属于该流程的节点 id → 报错，而不是静默返回错误结果"""
    token = ctx["admin"]["token"]
    f1 = make_flow(client, token, "工具测试流程-A")
    add_node(client, token, f1["id"], "A1", 1)
    f2 = make_flow(client, token, "工具测试流程-B")
    n2 = add_node(client, token, f2["id"], "B1", 1)

    actor = _actor(db, "regr_admin")
    result, _ = _call("flow_next_step", actor, {"flow_id": f1["id"], "current_node_id": n2["id"]})
    assert result["ok"] is False and "不属于该流程" in result["error"]


def test_get_item_permission_boundary(client, db, ctx):
    """越权读条目只得到「不存在或无权访问」，不泄露是否存在"""
    private_item = make_item(client, ctx["ua"]["token"], "工具测试条目-私有")
    shared_item = make_item(
        client, ctx["admin"]["token"], "工具测试条目-共享", visibility="all"
    )

    ua_actor = _actor(db, "regr_ua")
    ub_actor = _actor(db, "regr_ub")

    own, _ = _call("get_item", ua_actor, {"item_id": private_item["id"]})
    assert own["ok"] is True and own["item"]["title"] == "工具测试条目-私有"

    denied, _ = _call("get_item", ub_actor, {"item_id": private_item["id"]})
    assert denied["ok"] is False
    assert denied["error"] == "条目不存在或无权访问"

    # 管理员设了「全员共享」的内容，普通用户可读（沿用现有 read_scope）
    allowed, _ = _call("get_item", ub_actor, {"item_id": shared_item["id"]})
    assert allowed["ok"] is True


# ===== 4. 异常兜底 =====

def test_tool_exception_is_captured(db, ctx, monkeypatch):
    """底层 service 抛错 → ok=False + 轨迹记为失败，不冒泡打断对话"""
    def boom(*a, **k):
        raise RuntimeError("Qdrant 挂了")

    monkeypatch.setattr("app.services.chat_service.search_relevant_items", boom)

    actor = _actor(db, "regr_ua")
    result, trace = _call("knowledge_search", actor, {"query": "校招"})
    assert result["ok"] is False
    assert "RuntimeError" in result["error"] and "Qdrant 挂了" in result["error"]
    assert trace[0]["ok"] is False


# ===== 5. 写工具权限与约束 =====

def test_write_tools_staff_only(db, ctx, client):
    """普通用户调管理员写工具：直接权限错误，且不产生任何数据"""
    ua_actor = _actor(db, "regr_ua")
    before = len(body(client.get("/api/items", params={"limit": 200}, headers=hdr(ctx["admin"]["token"])))["items"])

    denied, trace = _call("create_draft_item", ua_actor, {"title": "普通用户不该能建"})
    assert denied["ok"] is False and "权限不足" in denied["error"]
    assert trace[0]["ok"] is False

    denied2, _ = _call("stats_snapshot", ua_actor, {})
    assert denied2["ok"] is False and "权限不足" in denied2["error"]

    after = len(body(client.get("/api/items", params={"limit": 200}, headers=hdr(ctx["admin"]["token"])))["items"])
    assert after == before, "被拒绝的调用不应写入任何数据"


def test_create_draft_item_forces_draft(client, db, ctx):
    """管理员建条目只能落成草稿（不会进入检索，必须人工发布）"""
    actor = _actor(db, "regr_admin")
    with agent_context(actor["user"], actor["db"], source="test"):
        result = _tool("create_draft_item").invoke(
            {
                "title": "工具测试草稿条目",
                "steps": ["第一步", "第二步"],
                "system_or_material": "格慧招",
                "caution": "注意时限",
            }
        )
    assert result["ok"] is True
    assert result["status"] == "draft"

    # 通过 API 复核：新条目存在且为 draft
    items = body(
        client.get(
            "/api/items", params={"keyword": "工具测试草稿条目", "limit": 5},
            headers=hdr(ctx["admin"]["token"]),
        )
    )["items"]
    assert items and items[0]["status"] == "draft"
    assert items[0]["steps"] == ["第一步", "第二步"]


def test_update_item_changes_fields_only(client, db, ctx):
    """update_item 能改内容字段，但不能改状态（发布属人工决策）"""
    item = make_item(client, ctx["admin"]["token"], "工具测试条目-待改", caution="旧注意事项")

    actor = _actor(db, "regr_admin")
    result, _ = _call("update_item", actor, {"item_id": item["id"], "caution": "新注意事项"})
    assert result["ok"] is True and result["changed"] == ["caution"]

    fetched, _ = _call("get_item", actor, {"item_id": item["id"]})
    assert fetched["item"]["caution"] == "新注意事项"
    assert fetched["item"]["status"] == "draft"

    # 没有可改字段时明确报错，而不是静默成功
    empty, _ = _call("update_item", actor, {"item_id": item["id"]})
    assert empty["ok"] is False and "没有提供任何" in empty["error"]

    # 不存在的条目
    missing, _ = _call("update_item", actor, {"item_id": "no-such-item", "title": "x"})
    assert missing["ok"] is False


# ===== 6. 备忘录：用户隔离 =====

def test_memo_create_and_list_isolation(client, db, ctx):
    """memo_create 写入本人；memo_list 只能看到自己的待办（跨用户隔离）"""
    ua_actor = _actor(db, "regr_ua")
    ub_actor = _actor(db, "regr_ub")

    created, _ = _call(
        "memo_create",
        ua_actor,
        {"title": "工具测试待办-UA", "priority": "high", "due_date": "2026-12-31T09:00:00"},
    )
    assert created["ok"] is True and created["priority"] == "high"

    ua_list, _ = _call("memo_list", ua_actor, {"keyword": "工具测试待办"})
    assert ua_list["count"] == 1 and ua_list["memos"][0]["title"] == "工具测试待办-UA"

    ub_list, _ = _call("memo_list", ub_actor, {"keyword": "工具测试待办"})
    assert ub_list["count"] == 0, "不应看到其他用户的备忘录"

    # 非法日期由 schema/校验层拦下，工具返回错误而不是写入脏数据
    bad, _ = _call("memo_create", ua_actor, {"title": "日期非法", "due_date": "不是日期"})
    assert bad["ok"] is False


# ===== 7. 轨迹汇总（供 M1 落库与前端展示）=====

def test_business_failure_is_traced_as_failure(client, db, ctx):
    """业务失败（未抛异常但 ok=False）也要记成失败调用，供质量度量使用"""
    token = ctx["admin"]["token"]
    empty_flow = make_flow(client, token, "工具测试流程-空节点")

    actor = _actor(db, "regr_admin")
    result, trace = _call("flow_next_step", actor, {"flow_id": empty_flow["id"]})
    assert result["ok"] is False and "没有配置任何节点" in result["error"]
    assert trace[0]["ok"] is False

    missing, trace2 = _call("get_item", actor, {"item_id": "no-such-item"})
    assert missing["ok"] is False
    assert trace2[0]["ok"] is False


def test_trace_summary(db, ctx, monkeypatch):
    """多次调用后 trace 完整可汇总：计数、失败数、耗时"""
    monkeypatch.setattr("app.services.chat_service.search_relevant_items", lambda *a, **k: [])

    actor = _actor(db, "regr_ua")
    with agent_context(actor["user"], actor["db"], source="test") as actx:
        _tool("knowledge_search").invoke({"query": "问题一"})
        _tool("knowledge_search").invoke({"query": "问题二"})
        _tool("memo_list").invoke({})
        summary = actx.summary()

    assert summary["tool_calls"] == 3
    assert summary["failed_calls"] == 0
    assert summary["user_id"] == actor["user"].id
    assert [s["tool"] for s in summary["steps"]] == [
        "knowledge_search",
        "knowledge_search",
        "memo_list",
    ]
    assert all("ms" in s and "args" in s for s in summary["steps"])


def test_context_is_cleaned_up_after_use(db, ctx):
    """上下文用后即还原，避免跨请求串号"""
    actor = _actor(db, "regr_ua")
    with agent_context(actor["user"], actor["db"], source="test"):
        assert current_context() is not None
    assert current_context() is None
