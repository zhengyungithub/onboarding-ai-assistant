"""文档解析 API"""
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException
from app.models import User
from app.core.deps import get_current_user
from app.services.document_service import parse_uploaded_file

router = APIRouter(prefix="/api/documents", tags=["文档解析"])


@router.post("/parse")
async def parse_document(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """上传文档并解析为 Markdown 文本"""
    allowed = {".doc", ".docx", ".xls", ".xlsx", ".pdf", ".txt", ".md", ".ppt", ".pptx"}
    ext = "." + file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext not in allowed:
        raise HTTPException(status_code=400, detail=f"不支持的文件格式: {ext}")

    contents = await file.read()
    try:
        markdown = parse_uploaded_file(contents, file.filename)
        return {"filename": file.filename, "content": markdown}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档解析失败: {str(e)}")
