"""文档确认清单：把「这份表格对不对」从群聊里捞出来，变成可追踪的记录

为什么需要这张表：
科室里一份报名表、一张排班表，通常是发到群里让相关人「看一下」。
看过没有、谁看过、谁觉得有问题——全散在聊天记录里，发起人只能反复追问。
这里把每一次确认拆成「一个文档 × 一个确认人 × 一条状态」，
发起人一眼能看到 3/5 已确认、谁还没看、谁提了问题。

关键设计：
- doc_version 是**发起时**的版本号快照。文档升版后旧版的确认记录不会被算进新版，
  否则「v2 改完还要重新确认」这件事就看不出来了。
- token 是免登录通道的凭据：确认人往往不装系统、也不一定有账号，
  没有这条通道，协同冷启动必然失败（这条通道默认关闭，见 DOC_CONFIRM_PUBLIC_ENABLED）。
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, Integer, ForeignKey
from app.db.session import Base


def generate_uuid() -> str:
    return str(uuid.uuid4())


# 状态取值（与前端约定的三态 + 撤销）
STATUS_PENDING = "pending"        # 已发起，等待确认
STATUS_CONFIRMED = "confirmed"    # 确认无误
STATUS_REJECTED = "rejected"      # 有问题（需附意见）
STATUS_CANCELLED = "cancelled"    # 发起人撤销（不计入统计分母）
STATUS_CHOICES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_REJECTED, STATUS_CANCELLED)


class DocConfirmation(Base):
    """文档确认记录：一个文档 × 一个确认人 = 一条（同版本内复用，不重复堆积）"""
    __tablename__ = "doc_confirmations"

    id = Column(String, primary_key=True, default=generate_uuid)
    document_id = Column(
        String, ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(String, nullable=False, index=True)       # 被邀请确认的人
    requested_by = Column(String, nullable=False)              # 发起人

    status = Column(String(20), nullable=False, default=STATUS_PENDING, index=True)
    comment = Column(Text, nullable=True)       # 确认意见（rejected 时必填）
    message = Column(Text, nullable=True)       # 发起时的附言，如「这版加了新岗位」
    doc_version = Column(Integer, nullable=False, default=1)   # 发起时的文档版本快照

    # 免登录通道：token 只对「这一个文档 + 这一个人」有效
    token = Column(String(64), nullable=True, unique=True, index=True)
    token_expires_at = Column(DateTime, nullable=True)
    first_viewed_at = Column(DateTime, nullable=True)   # 第一次打开确认页的时间（已读回执）

    created_at = Column(DateTime, default=datetime.now, index=True)
    resolved_at = Column(DateTime, nullable=True)
