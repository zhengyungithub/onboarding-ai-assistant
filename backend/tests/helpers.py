"""回归测试辅助函数：HTTP 调用封装、数据构造与断言

约定：
- 所有用例通过 FastAPI TestClient 在进程内调用，不启动真实服务；
- 不访问网络（Qdrant / Embedding / LLM 一律规避），保证离线可重复；
- 断言失败时打印期望状态码与实际响应体，便于定位。
"""
import json
from typing import List, Optional, Set

DEFAULT_PASSWORD = "regr-pass-123"


def hdr(token: str) -> dict:
    """构造鉴权请求头"""
    return {"Authorization": f"Bearer {token}"}


def expect(resp, code: int, label: str = ""):
    """断言 HTTP 状态码，失败时带出响应体"""
    assert resp.status_code == code, (
        f"{label}：期望 HTTP {code}，实际 {resp.status_code}；响应：{resp.text[:300]}"
    )
    return resp


def body(resp):
    try:
        return resp.json()
    except Exception:
        return None


# ===== 账号 =====

def login(client, username: str, password: str = DEFAULT_PASSWORD) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    expect(r, 200, f"登录 {username}")
    return r.json()["access_token"]


def raw_create_user(client, token: str, username: str, role: str = "user",
                    password: str = DEFAULT_PASSWORD):
    """创建用户（返回原始响应，供权限断言使用）"""
    return client.post(
        "/api/auth/users",
        json={"username": username, "password": password, "role": role},
        headers=hdr(token),
    )


def create_user(client, token: str, username: str, role: str = "user",
                password: str = DEFAULT_PASSWORD) -> dict:
    r = raw_create_user(client, token, username, role, password)
    expect(r, 201, f"创建用户 {username}({role})")
    return body(r)


def user_obj(db, username: str):
    """取用户 ORM 对象（用于直接调用服务层函数）"""
    from app.models import User
    return db.query(User).filter(User.username == username).first()


# ===== 知识条目 =====

def make_item(client, token: str, title: str, **extra) -> dict:
    payload = {"title": title, "status": "draft"}
    payload.update(extra)
    r = client.post("/api/items", json=payload, headers=hdr(token))
    expect(r, 201, f"创建知识条目「{title}」")
    return body(r)


def list_items(client, token: str, **params) -> List[dict]:
    r = client.get("/api/items", params={"limit": 200, **params}, headers=hdr(token))
    expect(r, 200, "查询知识条目列表")
    return body(r)["items"]


def item_ids(client, token: str, **params) -> Set[str]:
    return {i["id"] for i in list_items(client, token, **params)}


def publish_directly(db, item_id: str, status: str = "published"):
    """直接改库置为已发布：绕过向量化，避免测试依赖 Qdrant / Embedding API"""
    from app.models import KnowledgeItem
    item = db.query(KnowledgeItem).filter(KnowledgeItem.id == item_id).first()
    assert item is not None, f"条目不存在：{item_id}"
    item.status = status
    db.commit()
    db.expire_all()
    return item


# ===== 流程 =====

def make_flow(client, token: str, name: str, **extra) -> dict:
    payload = {"name": name}
    payload.update(extra)
    r = client.post("/api/flows", json=payload, headers=hdr(token))
    expect(r, 201, f"创建流程「{name}」")
    return body(r)


def list_flows(client, token: str) -> List[dict]:
    r = client.get("/api/flows", headers=hdr(token))
    expect(r, 200, "查询流程列表")
    return body(r)["flows"]


def flow_ids(client, token: str) -> Set[str]:
    return {f["id"] for f in list_flows(client, token)}


def add_node(client, token: str, flow_id: str, name: str, order: int,
             description: str = "", related_item_ids=None, **extra) -> dict:
    payload = {
        "name": name,
        "order": order,
        "description": description,
        "related_item_ids": related_item_ids or [],
    }
    # 工作流字段（depends_on / owner_id / is_gate 等）按需透传，不影响老用例
    payload.update({k: v for k, v in extra.items() if v is not None})
    r = client.post(f"/api/flows/{flow_id}/nodes", json=payload, headers=hdr(token))
    expect(r, 201, f"新增节点「{name}」")
    return body(r)


def update_node(client, token: str, flow_id: str, node_id: str, **fields) -> dict:
    r = client.put(f"/api/flows/{flow_id}/nodes/{node_id}", json=fields, headers=hdr(token))
    expect(r, 200, f"更新节点 {node_id}")
    return body(r)


# ===== 源文档 =====

def upload_doc(client, token: str, filename: str, content: str,
               visibility: Optional[str] = None,
               shared_user_ids: Optional[List[str]] = None,
               task_id: Optional[str] = None) -> dict:
    data = {}
    if visibility:
        data["visibility"] = visibility
    if shared_user_ids is not None:
        data["shared_user_ids"] = json.dumps(shared_user_ids)
    if task_id:
        data["task_id"] = task_id
    r = client.post(
        "/api/source-documents/upload",
        files={"file": (filename, content.encode("utf-8"), "text/plain")},
        data=data or None,
        headers=hdr(token),
    )
    expect(r, 200, f"上传文档「{filename}」")
    return body(r)


def doc_ids(client, token: str, **params) -> Set[str]:
    r = client.get("/api/source-documents", params={"limit": 200, **params}, headers=hdr(token))
    expect(r, 200, "查询文档列表")
    return {d["id"] for d in body(r)["items"]}


# ===== 对话消息（负反馈闭环用） =====

def make_chat_pair(db, user_id: str, question: str, answer: str,
                   feedback: Optional[str] = None, references=None, offset_seconds: int = 0):
    """写入一对「提问 + AI 回答」，返回回答消息的 id"""
    from datetime import datetime, timedelta
    from app.models import ChatMessage

    base = datetime.now() + timedelta(seconds=offset_seconds)
    q = ChatMessage(user_id=user_id, role="user", content=question, created_at=base)
    a = ChatMessage(
        user_id=user_id, role="assistant", content=answer, feedback=feedback,
        references=references or [],
        created_at=base + timedelta(seconds=1),
    )
    db.add_all([q, a])
    db.commit()
    db.refresh(a)
    return a.id


def clean_chat_messages(db, user_ids: List[str]):
    """清掉测试用户的对话消息"""
    from app.models import ChatMessage
    db.query(ChatMessage).filter(ChatMessage.user_id.in_(user_ids)).delete(
        synchronize_session=False
    )
    db.commit()
