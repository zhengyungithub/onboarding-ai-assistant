"""源文档数据模型：用户上传的原始文档/表格，存本地并入库管理"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer, Boolean, JSON
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


class SourceDocument(Base):
    """用户上传的原始文档（Word/Excel/PDF/TXT 等）"""
    __tablename__ = "source_documents"

    id = Column(String, primary_key=True, default=generate_uuid)
    user_id = Column(String, nullable=False, index=True)  # 多租户隔离

    filename = Column(String(500), nullable=False)        # 原始文件名
    file_path = Column(String(1000), nullable=False)      # 本地存储路径
    file_size = Column(Integer, default=0)                # 文件大小（字节）
    file_type = Column(String(50), default="")            # 文件扩展名

    content = Column(Text, nullable=True)                 # 解析后的 Markdown 文本
    status = Column(String(20), nullable=False, default="stored")  # stored | extracted
    extracted_count = Column(Integer, default=0)          # 已抽取的条目数

    # 版本台账（迭代 2）：同名同用户重复上传自动升版，旧版保留可下载
    #   version      本版版本号，新上传从 1 开始
    #   supersedes_id 本版取代的上一版 id（新版 → 旧版，v1 为 NULL）
    #                 "是否最新版" = 不存在其他记录以本记录为 supersedes_id
    version = Column(Integer, nullable=False, default=1, server_default="1")
    supersedes_id = Column(String, nullable=True, index=True)

    # 文档归属的任务（迭代 2）：群里的表格能对上"这是哪个活的材料"
    task_id = Column(String, nullable=True, index=True)

    # 可见范围（三态）：private 仅自己 | all 全员共享 | custom 定向共享
    # is_shared 为派生兼容值（= visibility != 'private'），由 apply_visibility 统一维护
    is_shared = Column(Boolean, nullable=False, default=False, index=True)
    visibility = Column(String(20), nullable=False, default="private", server_default="private")
    shared_user_ids = Column(JSON, nullable=True)  # 定向共享的目标用户 ID 列表

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
