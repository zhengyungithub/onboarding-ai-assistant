"""问答 API：同步 + 流式

Agent 灰度（M1）：
- `AGENT_MODE=off`（默认）：完全走原有固定管线，行为与改造前一字不差；
- `AGENT_MODE=on`：`/api/chat/stream` 改走 Agent，但 **SSE 协议保持不变**
  （先 references JSON 再纯文本），前端无需任何改动；
- `AGENT_MODE=shadow`：用户看固定管线结果，Agent 在后台只读跑一遍并记录轨迹，供质量对比。

富事件协议（含分诊/工具/校验过程）走新增的 `/api/chat/agent/stream`，
供后续「过程」面板使用，不影响现有前端。

引用校验（改造 3）的落地点：
- 首包的 references 是**候选引用**（保证前端第一个 data 就能显示来源）；
- 生成结束后 runtime 会把**校验过滤后的引用 + 覆盖率 + 轨迹**写进 `sink`，
  持久化时优先采用它——前端流结束后会重新拉取消息，那时看到的就是校验后的引用。
"""
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.schemas.knowledge import ChatRequest, ChatResponse
from app.services import chat_service
from app.services import chat_message_service
from app.core.deps import get_current_user
from app.agent import runtime as agent_runtime

router = APIRouter(prefix="/api/chat", tags=["问答"])


@router.post("", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = chat_service.get_answer_sync(current_user, req.question, db)
    # 持久化用户提问和 AI 回答
    user_msg = chat_message_service.create_message(db, current_user.id, "user", req.question)
    ai_msg = chat_message_service.create_message(
        db, current_user.id, "assistant", result["answer"], result.get("references")
    )
    # 记忆维护（改造 4）：与流式路径口径一致
    _after_answer(
        db,
        current_user,
        req.question,
        message_ids=[user_msg.id, ai_msg.id],
        answered=bool(result.get("references")),
        source="fixed-sync",
    )
    return result


@router.post("/stream")
def chat_stream(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    mode = agent_runtime.current_mode()

    if mode == "on":
        # Agent 路径：协议与固定管线一致，异常时 runtime 内部回落到固定管线
        return StreamingResponse(
            _agent_stream_with_persistence(current_user, req.question, db),
            media_type="text/event-stream",
        )

    if mode == "shadow":
        # 先按固定管线正常服务用户，再在后台只读跑一遍 Agent 记录轨迹
        agent_runtime.maybe_run_shadow(current_user.id, req.question)

    return StreamingResponse(
        _chat_stream_with_persistence(current_user, req.question, db),
        media_type="text/event-stream",
    )


@router.post("/agent/stream")
def chat_agent_stream(
    req: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent 富事件流（分诊 / 工具调用 / 引用 / 文本 / 结束）

    与 `/api/chat/stream` 的区别：多出过程事件。旧前端不消费该接口，
    因此这里可以自由扩展协议，不会影响现有页面。
    """
    return StreamingResponse(
        _agent_events_with_persistence(current_user, req.question, db),
        media_type="text/event-stream",
    )


async def _chat_stream_with_persistence(current_user: User, question: str, db: Session):
    """流式问答并在完成后持久化对话（原有实现 + 改造 4 的记忆维护）"""
    references = None
    full_content = ""
    first_chunk = True

    async for chunk in chat_service.get_answer_stream(current_user, question, db):
        # 每个 chunk 格式为 "data: xxx\n\n"，提取 data 部分
        data = ""
        if chunk.startswith("data: "):
            data = chunk[6:].rstrip("\n\n")

        if data == "[DONE]":
            pass  # 结束标记
        elif first_chunk and data:
            first_chunk = False
            try:
                parsed = json.loads(data)
                if "references" in parsed:
                    references = parsed["references"]
                else:
                    full_content += data
            except (json.JSONDecodeError, ValueError):
                full_content += data
        elif data:
            full_content += data

        yield chunk

    # 流式结束后持久化
    user_msg = chat_message_service.create_message(db, current_user.id, "user", question)
    ai_msg = chat_message_service.create_message(
        db, current_user.id, "assistant", full_content, references
    )
    # 记忆维护（改造 4）：向量化这轮对话、采集未答问题、必要时刷新摘要；全部异步
    _after_answer(
        db,
        current_user,
        question,
        message_ids=[user_msg.id, ai_msg.id],
        answered=bool(references),
        source="fixed",
    )


def _after_answer(db, user, question: str, *, message_ids, answered: bool, source: str) -> None:
    """一轮真实用户问答结束后的记忆维护（改造 4）

    刻意只在这里触发：记忆是**用户对话**的产物——管理员的调试运行（/api/agent/run）
    与 shadow 对比运行都不该污染它。全链路静默，任何失败都不影响用户。
    """
    try:
        from app.agent import memory

        memory.after_answer(
            db,
            user,
            question,
            message_ids=list(message_ids or []),
            answered=answered,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 记忆维护失败（不影响对话）: {exc}")


async def _agent_stream_with_persistence(current_user: User, question: str, db: Session):
    """Agent 路径的流式输出 + 对话持久化

    解析逻辑与固定管线共用同一套约定（首个 JSON 是 references，其余是文本），
    因此这里的收集规则与 `_chat_stream_with_persistence` 保持一致。
    生成结束后：用校验过滤后的引用与覆盖率持久化，并把 Agent 轨迹 id 回写到消息上。
    """
    references = None
    full_content = ""
    first_chunk = True
    sink: Dict[str, Any] = {}

    async for chunk in agent_runtime.stream_agent_legacy(current_user, question, db, sink=sink):
        data = ""
        if chunk.startswith("data: "):
            data = chunk[6:].rstrip("\n\n")

        if data == "[DONE]":
            pass
        elif first_chunk and data:
            first_chunk = False
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict) and "references" in parsed:
                    references = parsed["references"]
                else:
                    full_content += data
            except (json.JSONDecodeError, ValueError):
                full_content += data
        elif data:
            full_content += data

        yield chunk

    # 引用校验后的引用优先于首包的候选引用（宁少勿错）
    final_references = sink.get("references") or references
    outcome = sink.get("outcome")

    user_msg = chat_message_service.create_message(db, current_user.id, "user", question)
    msg = chat_message_service.create_message(
        db,
        current_user.id,
        "assistant",
        full_content,
        final_references,
        citation_coverage=sink.get("citation_coverage"),
        citation_dropped=sink.get("citation_dropped"),
    )
    _attach_agent_run(db, current_user.id, question, outcome, full_content, msg)
    # 记忆维护（改造 4）：Agent 路径同样走一遍（两路口径一致）
    _after_answer(
        db,
        current_user,
        question,
        message_ids=[user_msg.id, msg.id],
        answered=bool(final_references) and not bool(getattr(outcome, "short_circuit", "")),
        source="agent",
    )


def _attach_agent_run(db: Session, user_id: str, question: str, outcome, answer: str, msg) -> None:
    """把这次 Agent 运行落成轨迹，并把轨迹 id 回写到消息上（失败不影响问答）"""
    if outcome is None:
        return
    try:
        run = agent_runtime.record_run(db, user_id, question, outcome, mode="on", answer=answer)
        if run is not None and msg is not None:
            msg.agent_trace_id = run.id
            db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[警告] Agent 轨迹落库失败（不影响对话）: {exc}")


async def _agent_events_with_persistence(current_user: User, question: str, db: Session):
    """富事件流 + 对话持久化（只取文本事件入库，过程事件不入库）"""
    full_content = ""
    references = None
    sink: Dict[str, Any] = {}

    async for chunk in agent_runtime.stream_agent_events(current_user, question, db, sink=sink):
        data = ""
        if chunk.startswith("data: "):
            data = chunk[6:].rstrip("\n\n")
        if data and data != "[DONE]":
            try:
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    if parsed.get("type") == "references":
                        references = parsed.get("references")
                else:
                    full_content += data
            except (json.JSONDecodeError, ValueError):
                full_content += data
        yield chunk

    if full_content:
        final_references = sink.get("references") or references
        outcome = sink.get("outcome")
        user_msg = chat_message_service.create_message(db, current_user.id, "user", question)
        msg = chat_message_service.create_message(
            db,
            current_user.id,
            "assistant",
            full_content,
            final_references,
            citation_coverage=sink.get("citation_coverage"),
            citation_dropped=sink.get("citation_dropped"),
        )
        _attach_agent_run(db, current_user.id, question, outcome, full_content, msg)
        _after_answer(
            db,
            current_user,
            question,
            message_ids=[user_msg.id, msg.id],
            answered=bool(final_references) and not bool(getattr(outcome, "short_circuit", "")),
            source="agent",
        )
