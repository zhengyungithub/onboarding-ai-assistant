"""源文档 API：上传/列表/删除/AI 抽取"""
import os
import re
import json
import base64
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional, List
from app.db.session import get_db
from app.models import User
from app.core.deps import get_current_user, get_current_user_with_token_param
from app.core.roles import VISIBILITY_CHOICES
from app.services import source_document_service
from app.services import confirmation_service
from app.services.source_document_service import SourceDocument, IMAGES_DIR
from app.services.ingest_service import extract_from_document as extract_with_llm
from pydantic import BaseModel

router = APIRouter(prefix="/api/source-documents", tags=["源文档台账"])

ALLOWED_EXTS = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".txt", ".md", ".ppt", ".pptx"}

# 没有确认记录时的进度（前端统一按这个结构渲染，不必判空）
_EMPTY_CONFIRM_SUMMARY = {
    "total": 0, "confirmed": 0, "rejected": 0, "pending": 0, "all_confirmed": False,
}


def _attach_confirm_summaries(db: Session, docs: list) -> list:
    """批量挂上确认进度（非列属性）：文档列表/详情/任务材料都要显示「3/5 已确认」

    一次 group 查询，避免每行再查一次库。
    """
    if not docs:
        return docs
    summaries = confirmation_service.summary_map(db, [d.id for d in docs])
    for d in docs:
        d.confirm_summary = summaries.get(d.id) or dict(_EMPTY_CONFIRM_SUMMARY)
    return docs


class SourceDocumentResponse(BaseModel):
    id: str
    user_id: str = ""
    is_shared: bool = False
    visibility: str = "private"
    shared_user_ids: List[str] = []
    filename: str
    file_size: int
    file_type: str
    content: str = ""
    status: str
    extracted_count: int
    created_at: str
    # 版本台账 + 任务归属（迭代 2）
    version: int = 1
    supersedes_id: Optional[str] = None
    task_id: Optional[str] = None
    is_latest: bool = True          # 是否该版本链的最新版
    is_new_version: bool = False    # 本次上传是否触发了升版（仅上传接口为 true）
    # 三件套聚合（迭代 4）：这份材料的确认进度，任务详情里直接显示
    confirm_summary: Optional[dict] = None

    @classmethod
    def from_orm(cls, doc: SourceDocument):
        return cls(
            id=doc.id,
            user_id=doc.user_id,
            is_shared=bool(doc.is_shared),
            visibility=getattr(doc, "visibility", None) or "private",
            shared_user_ids=getattr(doc, "shared_user_ids", None) or [],
            filename=doc.filename,
            file_size=doc.file_size,
            file_type=doc.file_type,
            content=doc.content or "",
            status=doc.status,
            extracted_count=doc.extracted_count or 0,
            created_at=doc.created_at.isoformat() if doc.created_at else "",
            version=getattr(doc, "version", None) or 1,
            supersedes_id=getattr(doc, "supersedes_id", None),
            task_id=getattr(doc, "task_id", None),
            is_latest=getattr(doc, "is_latest", True),
            is_new_version=getattr(doc, "is_new_version", False),
            confirm_summary=getattr(doc, "confirm_summary", None),
        )


def _assert_task_owned(db: Session, user: User, task_id: str) -> None:
    """校验任务存在且属于当前用户（任务是个人的工作项，不允许挂到别人的任务上）"""
    from app.models import TaskInstance

    task = db.query(TaskInstance).filter(TaskInstance.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.user_id != user.id:
        raise HTTPException(status_code=403, detail="只能关联自己的任务")


class ExtractedItem(BaseModel):
    title: str = ""
    steps: List[str] = []
    system_or_material: str = ""
    caution: str = ""
    related_flow: str = ""


class ExtractResponse(BaseModel):
    items: List[ExtractedItem]
    content: str  # 返回解析后的全文，供前端预览/编辑


@router.post("/upload", response_model=SourceDocumentResponse)
async def upload_document(
    file: UploadFile = File(...),
    visibility: Optional[str] = Form(None),
    shared_user_ids: Optional[str] = Form(None),  # JSON 数组字符串
    task_id: Optional[str] = Form(None),          # 关联任务（可选）
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传文档：保存到本地 + 入库 + 解析内容；同名同用户重复上传自动升版"""
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")
    if visibility is not None and visibility not in VISIBILITY_CHOICES:
        raise HTTPException(status_code=400, detail=f"无效的可见范围: {visibility}")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="文件为空")

    try:
        shared_ids = json.loads(shared_user_ids) if shared_user_ids else None
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="shared_user_ids 格式错误")

    if task_id:
        _assert_task_owned(db, current_user, task_id)

    doc = source_document_service.save_upload(
        db, current_user, contents, file.filename,
        visibility=visibility, shared_user_ids=shared_ids, task_id=task_id or None,
    )
    _attach_confirm_summaries(db, [doc])
    return SourceDocumentResponse.from_orm(doc)


class VisibilityUpdateRequest(BaseModel):
    visibility: str
    shared_user_ids: Optional[List[str]] = None


@router.patch("/{doc_id}/visibility", response_model=SourceDocumentResponse)
def update_document_visibility(
    doc_id: str,
    data: VisibilityUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改文档可见范围（仅本人或管理员）"""
    if data.visibility not in VISIBILITY_CHOICES:
        raise HTTPException(status_code=400, detail=f"无效的可见范围: {data.visibility}")
    doc = source_document_service.update_visibility(
        db, doc_id, current_user, data.visibility, data.shared_user_ids
    )
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    source_document_service.apply_latest_flags(db, [doc])
    _attach_confirm_summaries(db, [doc])
    return SourceDocumentResponse.from_orm(doc)


@router.get("")
def list_documents(
    keyword: Optional[str] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    include_superseded: bool = Query(False, description="是否包含已被新版取代的历史版本"),
    task_id: Optional[str] = Query(None, description="只看关联到该任务的文档"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页查询当前用户的文档台账（默认只返回各版本链的最新版）"""
    total, items = source_document_service.list_documents(
        db, current_user, keyword=keyword, skip=skip, limit=limit,
        include_superseded=include_superseded, task_id=task_id,
    )
    _attach_confirm_summaries(db, items)
    return {
        "total": total,
        "items": [SourceDocumentResponse.from_orm(d) for d in items],
    }


class BindTaskRequest(BaseModel):
    task_id: Optional[str] = None


@router.patch("/{doc_id}/task", response_model=SourceDocumentResponse)
def bind_document_task(
    doc_id: str,
    data: BindTaskRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """关联/解除文档与任务的绑定（仅文档本人或管理员）"""
    if data.task_id:
        _assert_task_owned(db, current_user, data.task_id)
    doc = source_document_service.bind_task(db, doc_id, current_user, data.task_id)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    _attach_confirm_summaries(db, [doc])
    return SourceDocumentResponse.from_orm(doc)


@router.get("/{doc_id}/versions")
def list_document_versions(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取该文档所在版本链的全部版本（由旧到新）"""
    chain = source_document_service.list_versions(db, doc_id, current_user)
    if chain is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {
        "versions": [
            {
                "id": d.id,
                "version": getattr(d, "version", None) or 1,
                "filename": d.filename,
                "file_size": d.file_size or 0,
                "file_type": d.file_type or "",
                "status": d.status,
                "extracted_count": d.extracted_count or 0,
                "task_id": getattr(d, "task_id", None),
                "supersedes_id": getattr(d, "supersedes_id", None),
                "is_latest": getattr(d, "is_latest", True),
                "created_at": d.created_at.isoformat() if d.created_at else "",
            }
            for d in chain
        ]
    }


@router.get("/{doc_id}", response_model=SourceDocumentResponse)
def get_document(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取单个文档详情（含解析内容）"""
    doc = source_document_service.get_document(db, doc_id, current_user)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    source_document_service.apply_latest_flags(db, [doc])
    _attach_confirm_summaries(db, [doc])
    return SourceDocumentResponse.from_orm(doc)


@router.delete("/{doc_id}")
def delete_document(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除文档（同时删除本地文件）"""
    ok = source_document_service.delete_document(db, doc_id, current_user)
    if not ok:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True}


@router.get("/{doc_id}/download")
def download_document(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """下载原始文件（可用 WPS 打开）"""
    doc = source_document_service.get_document(db, doc_id, current_user)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    if not doc.file_path or not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="原始文件不存在")
    return FileResponse(
        path=doc.file_path,
        filename=doc.filename,
        media_type="application/octet-stream",
    )


@router.get("/{doc_id}/images/{image_name}")
def get_document_image(
    doc_id: str,
    image_name: str,
    current_user: User = Depends(get_current_user_with_token_param),
    db: Session = Depends(get_db),
):
    """获取文档中提取的图片（支持 token query 参数，供 <img> 标签使用）"""
    doc = source_document_service.get_document(db, doc_id, current_user)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    image_path = os.path.join(IMAGES_DIR, doc_id, image_name)
    if not os.path.exists(image_path):
        raise HTTPException(status_code=404, detail="图片不存在")

    # 根据扩展名推断 content-type
    ext = os.path.splitext(image_name)[1].lower()
    content_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
    }
    media_type = content_types.get(ext, "application/octet-stream")
    return FileResponse(path=image_path, media_type=media_type)


def _extract_images_from_markdown(doc_id: str, content: str) -> List[dict]:
    """从 Markdown 内容中提取图片，返回 base64 图片列表

    返回格式: [{"data": base64字符串, "type": "png"/"jpg", "name": "文件名"}]
    """
    images = []
    # 匹配 markdown 图片: ![alt](/api/source-documents/{doc_id}/images/{name})
    pattern = re.compile(
        r'!\[.*?\]\(/api/source-documents/' + re.escape(doc_id) + r'/images/([^)]+)\)'
    )
    seen = set()
    for match in pattern.finditer(content):
        image_name = match.group(1)
        if image_name in seen:
            continue
        seen.add(image_name)
        image_path = os.path.join(IMAGES_DIR, doc_id, image_name)
        if not os.path.exists(image_path):
            continue
        ext = os.path.splitext(image_name)[1].lower().lstrip(".")
        if ext not in ("png", "jpg", "jpeg", "gif", "bmp", "webp"):
            continue
        try:
            with open(image_path, "rb") as f:
                img_data = base64.b64encode(f.read()).decode("utf-8")
            images.append({"data": img_data, "type": ext, "name": image_name})
        except Exception as e:
            print(f"[警告] 读取图片失败 {image_name}: {e}")
    return images


@router.post("/{doc_id}/extract", response_model=ExtractResponse)
def extract_from_document(
    doc_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对已上传文档执行 AI 抽取（含图片），返回结构化条目草稿"""
    import traceback
    doc = source_document_service.get_document(db, doc_id, current_user)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    content = doc.content or ""
    if not content.strip():
        raise HTTPException(status_code=400, detail="文档内容为空，无法抽取")

    # 提取文档中的图片（base64），传给多模态 LLM
    images = _extract_images_from_markdown(doc_id, content)

    try:
        items = extract_with_llm(current_user, content, images=images if images else None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"抽取失败: {type(e).__name__}: {str(e)}")

    return ExtractResponse(items=items, content=content)


class MarkExtractedRequest(BaseModel):
    count: int = 0


@router.post("/{doc_id}/mark-extracted")
def mark_extracted(
    doc_id: str,
    req: MarkExtractedRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """标记文档为已抽取（入库成功后调用）"""
    doc = source_document_service.mark_extracted(db, doc_id, current_user, req.count)
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"ok": True}
