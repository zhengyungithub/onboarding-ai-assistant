"""迭代 4：文档确认清单（发起 / 待我确认 / 提交 / 撤销 / 免登录通道 / 级联）

守护点：
1. 只有文档本人或管理员能发起确认；别人的私有文档一律 404（不泄露存在性）；
2. 同文档 × 同人 × 仍是 pending 的重复发起只更新附言，不新增记录（统计分母不被灌水）；
   已确认的人再发起才算新一轮；
3. 只能处理「发给自己的」确认；重复提交、已处理再撤销都要报错；
4. rejected 必须带意见（"有问题"不说问题在哪没有意义）；
5. 统计口径 = 被邀请过的人（撤销的整条不计入），且按人取最新一轮；
6. 免登录通道默认关闭：关闭时一律 404，连"存不存在"都不暴露；
   开启后 token 只对「这一份文档 + 这一个人」有效，过期 410、撤销后 404；
7. 文档删除时确认记录级联清理，不在「待我确认」里留打不开的死记录。
"""
import uuid
from datetime import datetime, timedelta

import pytest

from tests.helpers import expect, body, hdr, create_user, login, upload_doc


def uniq(stem: str) -> str:
    return f"{stem}-{uuid.uuid4().hex[:6]}.txt"


@pytest.fixture
def public_on(monkeypatch):
    """打开免登录通道（默认关闭）；用例结束自动恢复"""
    from app.core.config import settings
    monkeypatch.setattr(settings, "DOC_CONFIRM_PUBLIC_ENABLED", True)
    return settings


@pytest.fixture
def public_off(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "DOC_CONFIRM_PUBLIC_ENABLED", False)
    return settings


@pytest.fixture
def scene(client, ctx):
    """一套独立场景：文档主人 owner + 三位确认人 + 一个无关用户"""
    def mk(tag):
        u = create_user(client, ctx["sa_token"], f"regr_conf_{tag}_{uuid.uuid4().hex[:4]}", "user")
        return {"id": u["id"], "username": u["username"], "token": login(client, u["username"])}

    owner, c1, c2, c3, outsider = (mk(t) for t in ("own", "c1", "c2", "c3", "out"))
    doc = upload_doc(client, owner["token"], uniq("报名表"), "岗位：运营\n人数：3\n截止：10-20")
    return {
        "owner": owner, "c1": c1, "c2": c2, "c3": c3, "out": outsider,
        "doc": doc,
    }


def _request(client, token, doc_id, user_ids, message=None):
    return client.post(
        f"/api/source-documents/{doc_id}/confirmations",
        json={"user_ids": user_ids, "message": message},
        headers=hdr(token),
    )


def _request_ok(client, token, doc_id, user_ids, message=None) -> dict:
    return body(expect(_request(client, token, doc_id, user_ids, message), 200, "发起确认"))


def _progress(client, token, doc_id):
    r = client.get(f"/api/source-documents/{doc_id}/confirmations", headers=hdr(token))
    return r, (body(r) if r.status_code == 200 else None)


def _mine(client, token, status="pending"):
    r = client.get("/api/confirmations/mine", params={"status": status}, headers=hdr(token))
    expect(r, 200, "待我确认列表")
    return body(r)


def _respond(client, token, conf_id, action, comment=None):
    return client.post(
        f"/api/confirmations/{conf_id}/respond",
        json={"action": action, "comment": comment},
        headers=hdr(token),
    )


def _item(res: dict, user_id: str) -> dict:
    return next(i for i in res["items"] if i["user_id"] == user_id)


# ===== 发起 =====

def test_request_creates_pending_for_each_confirmer(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"]], "这版加了新岗位，麻烦看一下")
    assert res["created"] == 2 and res["reused"] == 0
    assert res["public_enabled"] is False
    assert all(i["status"] == "pending" for i in res["items"])
    assert all(i["token"] is None for i in res["items"])  # 通道关闭不给 token
    assert _item(res, scene["c1"]["id"])["message"] == "这版加了新岗位，麻烦看一下"
    assert _item(res, scene["c1"]["id"])["doc_version"] == scene["doc"]["version"]


def test_request_filters_self_and_dedupes(client, scene, public_off):
    oid = scene["owner"]["id"]
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [oid, scene["c1"]["id"], scene["c1"]["id"]])
    assert len(res["items"]) == 1
    assert res["items"][0]["user_id"] == scene["c1"]["id"]


def test_request_only_self_is_rejected(client, scene, public_off):
    r = _request(client, scene["owner"]["token"], scene["doc"]["id"], [scene["owner"]["id"]])
    expect(r, 400, "只选自己被拒")


def test_request_empty_list_is_rejected(client, scene, public_off):
    r = _request(client, scene["owner"]["token"], scene["doc"]["id"], [])
    expect(r, 400, "空列表被拒")


def test_request_unknown_user_is_rejected(client, scene, public_off):
    r = _request(client, scene["owner"]["token"], scene["doc"]["id"], ["not-exist-user"])
    expect(r, 400, "不存在的用户被拒")


def test_request_others_private_doc_is_404(client, scene, public_off):
    r = _request(client, scene["out"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    expect(r, 404, "他人私有文档发起确认")


def test_request_twice_does_not_duplicate(client, scene, public_off):
    ids = [scene["c1"]["id"], scene["c2"]["id"]]
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], ids, "第一版")
    again = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], ids, "第二版")
    assert again["created"] == 0 and again["reused"] == 2
    _, prog = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert prog["summary"]["total"] == 2  # 分母没有翻倍
    assert all(i["message"] == "第二版" for i in prog["items"])


def test_request_again_after_confirm_starts_new_round(client, scene, public_off):
    first = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = first["items"][0]["id"]
    expect(_respond(client, scene["c1"]["token"], conf_id, "confirmed"), 200, "第一次确认")

    second = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    assert second["created"] == 1 and second["reused"] == 0
    _, prog = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert prog["summary"]["total"] == 1
    assert prog["summary"]["pending"] == 1       # 新一轮待确认
    assert prog["summary"]["confirmed"] == 0     # 旧结论被新轮次覆盖
    assert _item(prog, scene["c1"]["id"])["rounds"] == 2


# ===== 确认人视角 =====

def test_my_confirmations_contains_invitation(client, scene, public_off):
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    res = _mine(client, scene["c1"]["token"])
    assert res["total"] == 1
    assert res["items"][0]["filename"] == scene["doc"]["filename"]
    assert res["items"][0]["owner_name"] == scene["owner"]["username"]
    assert res["items"][0]["requested_by_name"] == scene["owner"]["username"]


def test_my_confirmations_empty_for_outsider(client, scene, public_off):
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    assert _mine(client, scene["out"]["token"])["total"] == 0


def test_pending_count(client, scene, public_off):
    before = body(client.get("/api/confirmations/pending-count", headers=hdr(scene["c1"]["token"])))["count"]
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    after = body(client.get("/api/confirmations/pending-count", headers=hdr(scene["c1"]["token"])))["count"]
    assert after == before + 1


def test_detail_visible_to_invitee_and_requester(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"]], "看一下名单")
    conf_id = res["items"][0]["id"]

    for who in ("c1", "owner"):
        r = client.get(f"/api/confirmations/{conf_id}", headers=hdr(scene[who]["token"]))
        data = body(expect(r, 200, f"{who} 查看确认详情"))
        assert "运营" in data["content"]        # 免登录/登录态都要能看到正文
        assert data["is_invitee"] is (who == "c1")
        assert data["is_requester"] is (who == "owner")


def test_detail_forbidden_for_outsider(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    r = client.get(f"/api/confirmations/{res['items'][0]['id']}", headers=hdr(scene["out"]["token"]))
    expect(r, 403, "无关用户看确认详情")


def test_respond_confirmed(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    data = body(expect(_respond(client, scene["c1"]["token"], conf_id, "confirmed"), 200, "确认无误"))
    assert data["status"] == "confirmed" and data["resolved_at"]


def test_respond_twice_is_conflict(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    expect(_respond(client, scene["c1"]["token"], conf_id, "confirmed"), 200, "首次确认")
    expect(_respond(client, scene["c1"]["token"], conf_id, "rejected", "晚了"), 409, "重复提交")


def test_rejected_requires_comment(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    expect(_respond(client, scene["c1"]["token"], conf_id, "rejected"), 400, "不填意见被拒")
    # 被拒后仍是 pending，可以补意见
    data = body(expect(
        _respond(client, scene["c1"]["token"], conf_id, "rejected", "人数应该是 4 人，漏了一个"),
        200, "补充意见",
    ))
    assert data["status"] == "rejected" and "4 人" in data["comment"]


def test_respond_by_other_invitee_is_forbidden(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"]])
    conf_id = _item(res, scene["c1"]["id"])["id"]
    expect(_respond(client, scene["c2"]["token"], conf_id, "confirmed"), 403, "替别人确认")


# ===== 撤销 =====

def test_revoke_by_requester(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"]])
    conf_id = _item(res, scene["c1"]["id"])["id"]
    data = body(expect(
        client.post(f"/api/confirmations/{conf_id}/revoke", headers=hdr(scene["owner"]["token"])),
        200, "撤销确认",
    ))
    assert data["status"] == "cancelled"
    _, prog = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert prog["summary"]["total"] == 1        # 撤销的不计入分母
    assert prog["summary"]["pending"] == 1


def test_revoke_by_invitee_is_forbidden(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    expect(
        client.post(f"/api/confirmations/{conf_id}/revoke", headers=hdr(scene["c1"]["token"])),
        403, "非发起人撤销",
    )


def test_revoke_after_confirmed_is_conflict(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    expect(_respond(client, scene["c1"]["token"], conf_id, "confirmed"), 200, "先确认")
    expect(
        client.post(f"/api/confirmations/{conf_id}/revoke", headers=hdr(scene["owner"]["token"])),
        409, "已处理不能撤销",
    )


# ===== 文档进度 / 聚合 =====

def test_document_progress_summary(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"], scene["c3"]["id"]], "三个都看下")
    expect(_respond(client, scene["c1"]["token"], _item(res, scene["c1"]["id"])["id"], "confirmed"), 200, "c1 确认")
    expect(_respond(client, scene["c2"]["token"], _item(res, scene["c2"]["id"])["id"],
                    "rejected", "截止日期写错了"), 200, "c2 提意见")

    _, prog = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    s = prog["summary"]
    assert (s["total"], s["confirmed"], s["rejected"], s["pending"]) == (3, 1, 1, 1)
    assert s["all_confirmed"] is False
    assert _item(prog, scene["c2"]["id"])["comment"] == "截止日期写错了"


def test_all_confirmed_flag(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"]])
    for who in ("c1", "c2"):
        expect(_respond(client, scene[who]["token"], _item(res, scene[who]["id"])["id"], "confirmed"),
               200, f"{who} 确认")
    _, prog = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert prog["summary"]["all_confirmed"] is True and prog["summary"]["pending"] == 0


def test_document_list_carries_confirm_summary(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    expect(_respond(client, scene["c1"]["token"], res["items"][0]["id"], "confirmed"), 200, "确认")
    r = client.get("/api/source-documents", params={"limit": 200}, headers=hdr(scene["owner"]["token"]))
    docs = body(expect(r, 200, "文档列表"))["items"]
    target = next(d for d in docs if d["id"] == scene["doc"]["id"])
    assert target["confirm_summary"]["total"] == 1
    assert target["confirm_summary"]["confirmed"] == 1
    assert target["confirm_summary"]["all_confirmed"] is True


def test_progress_of_others_private_doc_is_404(client, scene, public_off):
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    r, _ = _progress(client, scene["out"]["token"], scene["doc"]["id"])
    expect(r, 404, "他人文档进度")


# ===== 免登录通道 =====

def test_public_disabled_by_default(client, scene, public_off):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    assert res["items"][0]["token"] is None
    r = client.get(f"/api/public/confirmations/{'x' * 32}")
    expect(r, 404, "关闭时公开接口 404")

    # 关闭时也不能签发链接
    r = client.post(f"/api/confirmations/{res['items'][0]['id']}/link",
                    headers=hdr(scene["owner"]["token"]))
    expect(r, 400, "关闭时签发链接")


def test_public_link_and_view(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    token = res["items"][0]["token"]
    assert token and res["public_enabled"] is True

    r = client.get(f"/api/public/confirmations/{token}")
    data = body(expect(r, 200, "免登录查看"))
    assert data["status"] == "pending"
    assert data["filename"] == scene["doc"]["filename"]
    assert "运营" in data["content"]
    assert data["confirm_user_name"] == scene["c1"]["username"]
    # 不泄露内部 id
    assert "user_id" not in data and "owner_id" not in data


def test_public_view_marks_first_viewed(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    client.get(f"/api/public/confirmations/{res['items'][0]['token']}")

    detail = body(client.get(f"/api/confirmations/{conf_id}", headers=hdr(scene["owner"]["token"])))
    assert detail["first_viewed_at"]  # 发起人能看到「已打开」


def test_public_download(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    r = client.get(f"/api/public/confirmations/{res['items'][0]['token']}/download")
    expect(r, 200, "免登录下载")
    assert "运营".encode("utf-8") in r.content


def test_public_respond(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    token = res["items"][0]["token"]
    data = body(expect(
        client.post(f"/api/public/confirmations/{token}/respond",
                    json={"action": "confirmed"}),
        200, "免登录确认",
    ))
    assert data["status"] == "confirmed"
    # 已处理后再次提交冲突
    expect(
        client.post(f"/api/public/confirmations/{token}/respond", json={"action": "confirmed"}),
        409, "免登录重复提交",
    )


def test_public_bad_token_is_404(client, scene, public_on):
    expect(client.get("/api/public/confirmations/short"), 404, "过短 token")
    expect(client.get(f"/api/public/confirmations/{'a' * 32}"), 404, "不存在的 token")


def test_public_expired_token_is_410(client, scene, public_on, db):
    from app.models import DocConfirmation
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf = db.query(DocConfirmation).filter(DocConfirmation.id == res["items"][0]["id"]).first()
    conf.token_expires_at = datetime.now() - timedelta(days=1)
    db.commit()

    expect(client.get(f"/api/public/confirmations/{conf.token}"), 410, "过期链接")


def test_public_token_dead_after_revoke(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id, token = res["items"][0]["id"], res["items"][0]["token"]
    expect(client.post(f"/api/confirmations/{conf_id}/revoke",
                       headers=hdr(scene["owner"]["token"])), 200, "撤销")
    expect(client.get(f"/api/public/confirmations/{token}"), 404, "撤销后链接失效")


def test_issue_link_by_requester(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = res["items"][0]["id"]
    data = body(expect(
        client.post(f"/api/confirmations/{conf_id}/link", headers=hdr(scene["owner"]["token"])),
        200, "取回链接",
    ))
    assert data["token"]
    expect(client.get(f"/api/public/confirmations/{data['token']}"), 200, "链接可用")


def test_issue_link_by_invitee_is_forbidden(client, scene, public_on):
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    expect(
        client.post(f"/api/confirmations/{res['items'][0]['id']}/link",
                    headers=hdr(scene["c1"]["token"])),
        403, "确认人不能签发别人看到的链接",
    )


# ===== 级联删除 =====

def test_delete_document_cascades_confirmations(client, scene, public_off, db):
    from app.models import DocConfirmation
    res = _request_ok(client, scene["owner"]["token"], scene["doc"]["id"],
                      [scene["c1"]["id"], scene["c2"]["id"]])
    assert _mine(client, scene["c1"]["token"])["total"] == 1

    expect(client.delete(f"/api/source-documents/{scene['doc']['id']}",
                         headers=hdr(scene["owner"]["token"])), 200, "删除文档")

    left = db.query(DocConfirmation).filter(
        DocConfirmation.document_id == scene["doc"]["id"]
    ).count()
    assert left == 0
    assert _mine(client, scene["c1"]["token"])["total"] == 0
    assert _mine(client, scene["c1"]["token"], status="all")["total"] == 0


# ===== 联系人 / 升版提示 =====

def test_contacts_exclude_self_and_accessible_to_user(client, scene):
    r = client.get("/api/confirmations/contacts", headers=hdr(scene["c1"]["token"]))
    data = body(expect(r, 200, "普通用户拉取联系人"))
    ids = {u["id"] for u in data["items"]}
    assert scene["c1"]["id"] not in ids          # 不把自己列进去
    assert scene["c2"]["id"] in ids
    assert set(data["items"][0].keys()) == {"id", "username"}   # 不暴露角色/邮箱


def test_progress_flags_superseded_version(client, scene, public_off):
    """旧版上的确认结果不能代表最新版：进度接口要标记 is_latest=False"""
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    _, before = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert before["is_latest"] is True

    # 同名再上传 → 自动升版，旧版变成历史版本
    newer = upload_doc(client, scene["owner"]["token"], scene["doc"]["filename"], "岗位：运营\n人数：4")
    assert newer["version"] == 2

    _, after = _progress(client, scene["owner"]["token"], scene["doc"]["id"])
    assert after["is_latest"] is False


def test_doc_without_confirmations_has_empty_summary(client, scene):
    r = client.get("/api/source-documents", params={"limit": 200}, headers=hdr(scene["owner"]["token"]))
    docs = body(expect(r, 200, "文档列表"))["items"]
    target = next(d for d in docs if d["id"] == scene["doc"]["id"])
    assert target["confirm_summary"]["total"] == 0
    assert target["confirm_summary"]["all_confirmed"] is False


# ===== 参数校验 =====

def test_invalid_status_query(client, scene, public_off):
    r = client.get("/api/confirmations/mine", params={"status": "whatever"},
                   headers=hdr(scene["c1"]["token"]))
    expect(r, 400, "无效状态")


# ===== 原始文件下载（原版式预览数据源）=====

def _pending_conf_id(client, token) -> str:
    items = _mine(client, token)["items"]
    assert items, "应存在待确认记录"
    return items[0]["id"]


def test_download_document_by_invitee(client, scene, public_off):
    """被确认人虽然读不到私有文档，但确认记录本身就是访问凭据"""
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = _pending_conf_id(client, scene["c1"]["token"])
    r = client.get(f"/api/confirmations/{conf_id}/download", headers=hdr(scene["c1"]["token"]))
    data = expect(r, 200, "被确认人下载原始文件")
    assert "人数".encode("utf-8") in r.content


def test_download_document_by_requester(client, scene, public_off):
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = _pending_conf_id(client, scene["c1"]["token"])
    r = client.get(f"/api/confirmations/{conf_id}/download", headers=hdr(scene["owner"]["token"]))
    expect(r, 200, "发起人下载原始文件")


def test_download_document_forbidden_for_outsider(client, scene, public_off):
    """无关用户既不是发起人也不是被确认人 → 403（存在性都不泄露的口径下用 403 即可，
    因为该接口要求先持有 conf_id，不存在性已由 404 用例单独守护）"""
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = _pending_conf_id(client, scene["c1"]["token"])
    r = client.get(f"/api/confirmations/{conf_id}/download", headers=hdr(scene["out"]["token"]))
    expect(r, 403, "无关用户无权下载")


def test_download_document_unauthenticated(client, scene, public_off):
    _request_ok(client, scene["owner"]["token"], scene["doc"]["id"], [scene["c1"]["id"]])
    conf_id = _pending_conf_id(client, scene["c1"]["token"])
    r = client.get(f"/api/confirmations/{conf_id}/download")
    expect(r, 401, "未登录")
