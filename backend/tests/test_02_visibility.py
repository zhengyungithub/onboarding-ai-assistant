"""三态可见范围回归（private / all / custom）

守护点：
1. 普通用户创建的内容强制「仅自己」，其他普通用户不可见（含 AI 检索）；
2. 管理员/超管新建默认「全员共享」，全员可见可检索，但对普通用户只读；
3. 「定向共享」仅被选中的用户可见，未选中者不可见；
4. 「指定用户」未选任何人时收敛为「仅自己」；
5. 可见范围可随时修改并立即生效；
6. AI 检索（关键词降级路径）严格遵循同一套可见范围。
"""
from tests.helpers import (
    expect, hdr, list_items, item_ids, make_item, make_flow, flow_ids,
    upload_doc, doc_ids, publish_directly, user_obj,
)

# 所有可检索内容共用一个词，保证降级检索能稳定命中
TOKEN = "清点"


def _ids(rows):
    return {r["id"] for r in rows}


# ===== 知识条目 =====

def test_normal_user_content_forced_private(client, ctx):
    """普通用户即便提交 visibility=all，也会被强制收敛为「仅自己」"""
    it = make_item(client, ctx["ua"]["token"], f"入职材料{TOKEN}流程-普通用户甲",
                   visibility="all", shared_user_ids=[ctx["ub"]["user"]["id"]])
    assert it["visibility"] == "private", f"普通用户内容应为 private，实际 {it['visibility']}"
    assert it["is_shared"] is False, "普通用户内容不应标记为共享"
    assert not it["shared_user_ids"], "普通用户内容不应带定向共享名单"


def test_private_item_invisible_to_other_normal_user(client, ctx):
    """普通用户的私有内容：其他普通用户列表与详情都不可见"""
    it = make_item(client, ctx["ua"]["token"], f"入职材料{TOKEN}流程-甲私有专属")

    assert it["id"] not in item_ids(client, ctx["ub"]["token"]), "乙不应看到甲的私有条目"
    expect(client.get(f"/api/items/{it['id']}", headers=hdr(ctx["ub"]["token"])),
           404, "乙读取甲的私有条目详情")
    # 本人可见
    assert it["id"] in item_ids(client, ctx["ua"]["token"]), "甲应能看到自己的条目"


def test_admin_created_item_defaults_to_all(client, ctx):
    """管理员新建内容默认「全员共享」，所有普通用户可见"""
    it = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-管理员默认")
    assert it["visibility"] == "all", f"管理员新建内容应默认全员共享，实际 {it['visibility']}"
    assert it["is_shared"] is True, "全员共享应同时维护 is_shared=True"

    for who in ("ua", "ub", "uc"):
        assert it["id"] in item_ids(client, ctx[who]["token"]), f"{who} 应能看到全员共享条目"
        expect(client.get(f"/api/items/{it['id']}", headers=hdr(ctx[who]["token"])),
               200, f"{who} 读取全员共享条目详情")


def test_shared_item_is_readonly_for_normal_user(client, ctx):
    """共享内容对普通用户只读：能读、不能改、不能删"""
    it = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-只读校验")
    token = ctx["ua"]["token"]

    expect(client.get(f"/api/items/{it['id']}", headers=hdr(token)), 200, "普通用户读取共享条目")
    expect(client.put(f"/api/items/{it['id']}", json={"title": "篡改"}, headers=hdr(token)),
           404, "普通用户修改共享条目")
    expect(client.delete(f"/api/items/{it['id']}", headers=hdr(token)),
           404, "普通用户删除共享条目")
    # 条目未被改动
    detail = client.get(f"/api/items/{it['id']}", headers=hdr(ctx["admin"]["token"])).json()
    assert detail["title"] == it["title"], "共享条目不应被普通用户改动"


def test_custom_sharing_only_visible_to_selected(client, ctx):
    """定向共享：只有被选中的用户可见（单选/多选）"""
    ua_id, ub_id = ctx["ua"]["user"]["id"], ctx["ub"]["user"]["id"]
    it = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-定向给甲",
                   visibility="custom", shared_user_ids=[ua_id])
    assert it["visibility"] == "custom"
    assert it["shared_user_ids"] == [ua_id], f"定向名单应只含甲，实际 {it['shared_user_ids']}"

    assert it["id"] in item_ids(client, ctx["ua"]["token"]), "被选中的甲应可见"
    assert it["id"] not in item_ids(client, ctx["ub"]["token"]), "未被选中的乙不应可见"
    assert it["id"] not in item_ids(client, ctx["uc"]["token"]), "未被选中的丙不应可见"
    expect(client.get(f"/api/items/{it['id']}", headers=hdr(ctx["ub"]["token"])),
           404, "乙读取定向共享条目")
    # 被共享者同样只读
    expect(client.put(f"/api/items/{it['id']}", json={"title": "篡改"}, headers=hdr(ctx["ua"]["token"])),
           404, "被共享者修改定向共享条目")

    # 多选：同时共享给甲和乙
    both = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-定向给甲乙",
                     visibility="custom", shared_user_ids=[ua_id, ub_id])
    for who in ("ua", "ub"):
        assert both["id"] in item_ids(client, ctx[who]["token"]), f"多选目标 {who} 应可见"
    assert both["id"] not in item_ids(client, ctx["uc"]["token"]), "多选之外的人不应可见"


def test_custom_without_targets_collapses_to_private(client, ctx):
    """选了「指定用户」却没选人 → 收敛为「仅自己」，不产生“谁都看不到又不安全”的中间态"""
    it = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-定向空名单",
                   visibility="custom", shared_user_ids=[])
    assert it["visibility"] == "private", f"空定向名单应收敛为 private，实际 {it['visibility']}"
    assert it["id"] not in item_ids(client, ctx["ua"]["token"]), "普通用户不应看到该条目"


def test_visibility_change_takes_effect_immediately(client, ctx):
    """可见范围可修改且立即生效：私有 → 全员共享"""
    it = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-改范围",
                   visibility="private")
    assert it["id"] not in item_ids(client, ctx["ua"]["token"]), "私有阶段普通用户不应可见"

    r = client.put(f"/api/items/{it['id']}", json={"visibility": "all"}, headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员改为全员共享")
    assert r.json()["visibility"] == "all"
    assert it["id"] in item_ids(client, ctx["ua"]["token"]), "全员共享后普通用户应立即可见"

    # 再收回为仅自己
    r = client.put(f"/api/items/{it['id']}", json={"visibility": "private"}, headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员收回为仅自己")
    assert it["id"] not in item_ids(client, ctx["ua"]["token"]), "收回后普通用户不应再可见"


# ===== 流程模板 =====

def test_flow_visibility_same_rules(client, ctx):
    """流程模板遵循与知识条目完全一致的可见范围规则"""
    ua_id = ctx["ua"]["user"]["id"]

    normal = make_flow(client, ctx["ua"]["token"], f"{TOKEN}流程-普通用户建", visibility="all")
    assert normal["visibility"] == "private", "普通用户的流程应强制仅自己"

    shared = make_flow(client, ctx["admin"]["token"], f"{TOKEN}流程-全员共享")
    assert shared["visibility"] == "all", "管理员新建流程应默认全员共享"

    custom = make_flow(client, ctx["admin"]["token"], f"{TOKEN}流程-定向给甲",
                       visibility="custom", shared_user_ids=[ua_id])
    assert custom["visibility"] == "custom"

    assert shared["id"] in flow_ids(client, ctx["ua"]["token"]), "全员共享流程全员可见"
    assert custom["id"] in flow_ids(client, ctx["ua"]["token"]), "定向流程对被选者可见"
    assert custom["id"] not in flow_ids(client, ctx["ub"]["token"]), "定向流程对未选者不可见"
    assert normal["id"] not in flow_ids(client, ctx["ub"]["token"]), "普通用户流程互不可见"


def test_flow_shared_is_readonly_for_normal_user(client, ctx):
    """共享流程对普通用户只读"""
    flow = make_flow(client, ctx["admin"]["token"], f"{TOKEN}流程-只读校验")
    token = ctx["ua"]["token"]
    expect(client.get(f"/api/flows/{flow['id']}", headers=hdr(token)), 200, "普通用户读取共享流程")
    expect(client.put(f"/api/flows/{flow['id']}", json={"name": "篡改"}, headers=hdr(token)),
           404, "普通用户修改共享流程")
    expect(client.delete(f"/api/flows/{flow['id']}", headers=hdr(token)),
           404, "普通用户删除共享流程")


# ===== 资料文档 =====

def test_document_visibility_and_update(client, ctx):
    """文档上传即带可见范围，且可后续修改；越权修改返回 404"""
    ua_id = ctx["ua"]["user"]["id"]

    admin_all = upload_doc(client, ctx["admin"]["token"], "入职材料清点清单-全员.txt",
                           "入职材料清点清单：工牌、电脑、门禁卡。", visibility="all")
    assert admin_all["visibility"] == "all"
    assert admin_all["id"] in doc_ids(client, ctx["ua"]["token"]), "全员共享文档普通用户应可见"

    admin_custom = upload_doc(client, ctx["admin"]["token"], "清点清单-定向给甲.txt",
                              "清点清单（定向）：仅甲可见。",
                              visibility="custom", shared_user_ids=[ua_id])
    assert admin_custom["id"] in doc_ids(client, ctx["ua"]["token"]), "定向文档对被选者可见"
    assert admin_custom["id"] not in doc_ids(client, ctx["ub"]["token"]), "定向文档对未选者不可见"

    ua_doc = upload_doc(client, ctx["ua"]["token"], "清点清单-普通用户私有.txt",
                        "清点清单（私有）：仅本人可见。")
    assert ua_doc["visibility"] == "private", "普通用户上传的文档应仅自己可见"
    assert ua_doc["id"] not in doc_ids(client, ctx["ub"]["token"]), "私有文档其他普通用户不可见"

    # 越权修改可见范围
    expect(
        client.patch(f"/api/source-documents/{admin_all['id']}/visibility",
                     json={"visibility": "private"}, headers=hdr(ctx["ua"]["token"])),
        404, "普通用户修改管理员文档的可见范围",
    )
    expect(
        client.patch(f"/api/source-documents/{admin_all['id']}/visibility",
                     json={"visibility": "bogus"}, headers=hdr(ctx["admin"]["token"])),
        400, "非法可见范围取值",
    )


def test_normal_user_document_stays_private_after_patch(client, ctx):
    """普通用户自己改可见范围也只能是「仅自己」（不能把私有内容推给全员）"""
    doc = upload_doc(client, ctx["ua"]["token"], "清点清单-普通用户改范围.txt", "清点内容。")
    r = client.patch(f"/api/source-documents/{doc['id']}/visibility",
                     json={"visibility": "all"}, headers=hdr(ctx["ua"]["token"]))
    expect(r, 200, "普通用户修改自己的文档可见范围")
    assert r.json()["visibility"] == "private", "普通用户不能被提升为全员共享"


def test_admin_can_share_document_after_upload(client, ctx):
    """管理员上传为私有后，可再改为全员共享并生效"""
    doc = upload_doc(client, ctx["admin"]["token"], "清点清单-后续共享.txt", "清点内容。",
                     visibility="private")
    assert doc["id"] not in doc_ids(client, ctx["ua"]["token"])

    r = client.patch(f"/api/source-documents/{doc['id']}/visibility",
                     json={"visibility": "all"}, headers=hdr(ctx["admin"]["token"]))
    expect(r, 200, "管理员改为全员共享")
    assert doc["id"] in doc_ids(client, ctx["ua"]["token"]), "共享后普通用户应可见"


# ===== AI 检索范围 =====

def _offline_embeddings(monkeypatch):
    """让向量检索必然失败，从而稳定走「关键词降级检索」这条路（离线可跑）"""
    from app.services import chat_service

    def _boom(*args, **kwargs):
        raise RuntimeError("embedding disabled in tests")

    monkeypatch.setattr(chat_service, "embed_dense", _boom)


def test_retrieval_scope_follows_visibility(client, ctx, db, monkeypatch):
    """AI 检索范围 = 自己的 + 全员共享 + 定向共享给自己的；不得越界"""
    _offline_embeddings(monkeypatch)
    from app.services import chat_service

    ua_id, ub_id = ctx["ua"]["user"]["id"], ctx["ub"]["user"]["id"]

    admin_all = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-全员可检索",
                          visibility="all")
    admin_custom = make_item(client, ctx["admin"]["token"], f"入职材料{TOKEN}流程-定向可检索",
                             visibility="custom", shared_user_ids=[ua_id])
    ub_private = make_item(client, ctx["ub"]["token"], f"入职材料{TOKEN}流程-乙私有不可检索")

    for it in (admin_all, admin_custom, ub_private):
        publish_directly(db, it["id"])

    question = f"入职材料{TOKEN}流程"
    ua = user_obj(db, ctx["ua"]["user"]["username"])
    ub = user_obj(db, ctx["ub"]["user"]["username"])
    admin = user_obj(db, ctx["admin"]["user"]["username"])

    ua_hits = _ids(chat_service.search_relevant_items(ua, question, db=db, limit=10))
    assert admin_all["id"] in ua_hits, "普通用户应能检索到全员共享的知识"
    assert admin_custom["id"] in ua_hits, "普通用户应能检索到定向共享给自己的知识"
    assert ub_private["id"] not in ua_hits, "普通用户不应检索到他人的私有知识"

    ub_hits = _ids(chat_service.search_relevant_items(ub, question, db=db, limit=10))
    assert admin_custom["id"] not in ub_hits, "定向共享对未被选中的人不可见（检索同样不可见）"

    admin_hits = _ids(chat_service.search_relevant_items(admin, question, db=db, limit=10))
    assert ub_private["id"] in admin_hits, "管理员应能检索到全局所有知识"
    assert admin_all["id"] in admin_hits
