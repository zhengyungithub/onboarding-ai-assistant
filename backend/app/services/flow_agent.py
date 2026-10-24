"""下一步提醒服务：用 LangGraph 包装流程节点状态流转"""
from typing import Optional, List, Annotated
from typing_extensions import TypedDict
from sqlalchemy.orm import Session
from langgraph.graph import StateGraph, END
from app.models import User, FlowNode
from app.services.flow_service import get_next_node, get_related_items
from app.services.llm_service import get_llm_for_user
from langchain_core.messages import HumanMessage, SystemMessage


class FlowState(TypedDict):
    """流程状态"""
    flow_id: str
    current_node_id: str
    user_id: str
    user: Optional[object]  # 当前用户对象（用于按角色判定流程可见范围）
    next_node: Optional[dict]
    related_items: List[dict]
    advice: str


def find_next_node(state: FlowState, db: Session) -> FlowState:
    """节点1：查找下一个节点"""
    next_node = get_next_node(db, state["flow_id"], state["current_node_id"], state["user"])
    if next_node:
        state["next_node"] = next_node  # 返回完整 ORM 对象
        state["related_items"] = get_related_items(db, next_node)
    else:
        state["next_node"] = None
        state["related_items"] = []
    return state


def should_end(state: FlowState) -> str:
    """条件边：判断是否有下一个节点"""
    if state["next_node"]:
        return "generate_advice"
    return END


def generate_advice(state: FlowState, user: User) -> FlowState:
    """节点2：用 LLM 生成操作要点"""
    next_node = state["next_node"]
    if not next_node:
        state["advice"] = "已经是最后一个步骤了，流程已完成！"
        return state

    items_text = ""
    if state["related_items"]:
        items_text = "\n关联知识条目：\n" + "\n".join(
            f"- {i['title']}" for i in state["related_items"]
        )

    prompt = f"""你是一个新员工入职流程助手。用户刚完成了当前步骤，现在需要进入下一步。

下一步信息：
- 步骤名称：{next_node.name}
- 步骤描述：{next_node.description or "无"}
{items_text}

请用简洁的语言提醒用户下一步需要做什么，包括操作要点和注意事项。不超过100字。"""

    try:
        llm = get_llm_for_user(user)
        response = llm.invoke([
            SystemMessage(content="你是流程助手，简洁地提醒用户下一步操作。"),
            HumanMessage(content=prompt),
        ])
        state["advice"] = response.content
    except Exception as e:
        state["advice"] = f"下一步：{next_node.name}。{next_node.description or ''}"

    return state


def build_flow_graph():
    """构建流程图状态机"""
    graph = StateGraph(FlowState)
    graph.add_node("find_next", find_next_node)
    graph.add_node("generate_advice", generate_advice)
    graph.set_entry_point("find_next")
    graph.add_conditional_edges("find_next", should_end)
    graph.add_edge("generate_advice", END)
    return graph.compile()


def get_next_step_advice(db: Session, user: User, flow_id: str, current_node_id: str) -> dict:
    """获取下一步提醒（完整流程）"""
    # 直接执行逻辑（不用 LangGraph 编译也可以，但为了展示 Agent 能力）
    state: FlowState = {
        "flow_id": flow_id,
        "current_node_id": current_node_id,
        "user_id": user.id,
        "user": user,
        "next_node": None,
        "related_items": [],
        "advice": "",
    }

    state = find_next_node(state, db)
    if state["next_node"]:
        state = generate_advice(state, user)
    else:
        state["advice"] = "已经是最后一个步骤了，流程已完成！"

    return {
        "next_node": state["next_node"],
        "related_items": state["related_items"],
        "advice": state["advice"],
    }
