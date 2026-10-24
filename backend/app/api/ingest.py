"""资料入库 API：文本 → 结构化抽取 → 批量入库"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models import User
from app.core.deps import get_current_user
from app.services.ingest_service import extract_from_text
from app.services.knowledge_service import create_item
from app.schemas.knowledge import KnowledgeItemCreate, KnowledgeItemResponse

router = APIRouter(prefix="/api/ingest", tags=["资料入库"])


class IngestRequest(BaseModel):
    """结构化抽取请求"""
    raw_text: str = Field(..., min_length=1, description="原始资料文本")


class ExtractedItem(BaseModel):
    """抽取后的单个条目（草稿）"""
    title: str = ""
    steps: List[str] = []
    system_or_material: str = ""
    caution: str = ""
    related_flow: str = ""


class IngestResponse(BaseModel):
    """抽取响应：返回多条草稿供用户校对"""
    items: List[ExtractedItem]


class BatchCreateRequest(BaseModel):
    """批量入库请求：用户校对后的条目列表"""
    items: List[KnowledgeItemCreate]


class BatchCreateResponse(BaseModel):
    """批量入库响应"""
    created: List[KnowledgeItemResponse]
    count: int


@router.post("/extract", response_model=IngestResponse)
def extract(
    req: IngestRequest,
    current_user: User = Depends(get_current_user),
):
    """从原始文本中抽取结构化条目（草稿）"""
    import traceback
    try:
        items = extract_from_text(current_user, req.raw_text)
        return IngestResponse(items=[ExtractedItem(**item) for item in items])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"抽取失败: {type(e).__name__}: {str(e)}")


@router.post("/batch", response_model=BatchCreateResponse)
def batch_create(
    req: BatchCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量入库：用户校对后的条目列表"""
    created = []
    for item_data in req.items:
        item = create_item(db, current_user, item_data)
        created.append(KnowledgeItemResponse.model_validate(item))
    return BatchCreateResponse(created=created, count=len(created))
