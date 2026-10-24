"""流程 API：CRUD + 节点管理 + 下一步提醒"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.core.deps import get_current_user
from app.services import flow_service, flow_agent
from app.schemas.flow import (
    FlowCreate, FlowUpdate, FlowResponse, FlowListResponse,
    FlowNodeCreate, FlowNodeResponse, NextStepRequest, NextStepResponse,
)

router = APIRouter(prefix="/api/flows", tags=["流程"])


@router.get("", response_model=FlowListResponse)
def list_flows(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    total, flows = flow_service.get_flows(db, current_user, skip, limit)
    return FlowListResponse(
        total=total,
        flows=[FlowResponse.model_validate(f) for f in flows],
    )


@router.post("", response_model=FlowResponse, status_code=201)
def create_flow(
    data: FlowCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    flow = flow_service.create_flow(db, current_user, data)
    return FlowResponse.model_validate(flow)


@router.get("/{flow_id}", response_model=FlowResponse)
def get_flow(
    flow_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    flow = flow_service.get_flow(db, flow_id, current_user)
    if not flow:
        raise HTTPException(status_code=404, detail="流程不存在")
    return FlowResponse.model_validate(flow)


@router.put("/{flow_id}", response_model=FlowResponse)
def update_flow(
    flow_id: str,
    data: FlowUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    flow = flow_service.update_flow(db, flow_id, current_user, data)
    if not flow:
        raise HTTPException(status_code=404, detail="流程不存在")
    return FlowResponse.model_validate(flow)


@router.delete("/{flow_id}", status_code=204)
def delete_flow(
    flow_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not flow_service.delete_flow(db, flow_id, current_user):
        raise HTTPException(status_code=404, detail="流程不存在")


# 节点管理
@router.post("/{flow_id}/nodes", response_model=FlowNodeResponse, status_code=201)
def add_node(
    flow_id: str,
    data: FlowNodeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = flow_service.add_node(db, flow_id, current_user, data)
    if not node:
        raise HTTPException(status_code=404, detail="流程不存在")
    return FlowNodeResponse.model_validate(node)


@router.put("/{flow_id}/nodes/{node_id}", response_model=FlowNodeResponse)
def update_node(
    flow_id: str,
    node_id: str,
    data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = flow_service.update_node(db, node_id, current_user, data)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    return FlowNodeResponse.model_validate(node)


@router.delete("/{flow_id}/nodes/{node_id}", status_code=204)
def delete_node(
    flow_id: str,
    node_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not flow_service.delete_node(db, node_id, current_user):
        raise HTTPException(status_code=404, detail="节点不存在")


# 下一步提醒
@router.post("/next-step", response_model=NextStepResponse)
def get_next_step(
    req: NextStepRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取下一步操作提醒"""
    result = flow_agent.get_next_step_advice(
        db, current_user, req.flow_id, req.current_node_id
    )
    return NextStepResponse(**result)
