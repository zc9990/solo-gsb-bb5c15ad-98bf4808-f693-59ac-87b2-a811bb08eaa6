"""POST /documents/{id}/reverts 撤销接口端到端集成测试。

每个用例使用独立临时 SQLite 文件。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content=None, doc_id="cfg", author=None):
    body = {"id": doc_id}
    if content is not None:
        body["content"] = content
    if author is not None:
        body["author"] = author
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch, author=None):
    body = {"base_revision": base_revision, "request_id": request_id, "patch": patch}
    if author is not None:
        body["author"] = author
    return client.patch(f"/documents/{doc_id}", json=body)


def _revert(client, doc_id, revision, request_id, author=None):
    body = {"revision": revision, "request_id": request_id}
    if author is not None:
        body["author"] = author
    return client.post(f"/documents/{doc_id}/reverts", json=body)


# ---------------- 基本成功路径 ----------------


def test_revert_latest_revision_restores_prior_snapshot(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 10, "c": 3}).status_code == 200
    # 当前 rev2：撤销 rev2 → 回到 rev1 的内容
    r = _revert(client, "cfg", 2, "undo-r2", author="alice")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 3
    assert body["reverted_revision"] == 2
    assert body["replayed"] is False
    assert body["changed"] is True
    assert body["idempotent"] is False
    assert body["content"] == {"a": 1, "b": 2}

    doc = client.get("/documents/cfg").json()
    assert doc["revision"] == 3
    assert doc["content"] == {"a": 1, "b": 2}

    # 撤销生成的是普通可追溯修订：历史详情记录逆向补丁 / 基线 / request_id / author
    rev3 = client.get("/documents/cfg/revisions/3").json()
    assert rev3["request_id"] == "undo-r2"
    assert rev3["base_revision"] == 2
    assert rev3["author"] == "alice"
    assert rev3["replayed"] is False
    # 逆向补丁：a 改回 1、删除 c
    assert rev3["patch"] == {"a": 1, "c": None}


def test_revert_deletion_restores_keys(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "del", {"a": None}).status_code == 200
    r = _revert(client, "cfg", 2, "undo-del")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1, "b": 2}


def test_revert_non_object_root_revision(client):
    _create(client, {"a": {"b": 1}})
    assert _patch(client, "cfg", 1, "to-list", [1, 2, 3]).status_code == 200
    r = _revert(client, "cfg", 2, "undo-list")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": {"b": 1}}


def test_revert_missing_document_404(client):
    r = _revert(client, "ghost", 2, "undo")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


# ---------------- 目标修订校验（422） ----------------


def test_revert_revision_one_is_422(client):
    _create(client, {"a": 1})
    r = _revert(client, "cfg", 1, "undo-1")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "invalid_revert_revision"
    assert detail["revision"] == 1
    assert detail["current_revision"] == 1
    # 不写修订
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_revert_unknown_revision_422(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": 2}).status_code == 200
    for bad in (99, 3):
        r = _revert(client, "cfg", bad, f"undo-{bad}")
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["error"] == "invalid_revert_revision"
        assert detail["revision"] == bad
        assert detail["current_revision"] == 2
    assert client.get("/documents/cfg").json()["revision"] == 2


def test_revert_revision_zero_rejected_by_model(client):
    _create(client, {"a": 1})
    r = client.post(
        "/documents/cfg/reverts", json={"revision": 0, "request_id": "x"}
    )
    assert r.status_code == 422


def test_revert_extra_field_rejected(client):
    _create(client, {"a": 1})
    r = client.post(
        "/documents/cfg/reverts",
        json={"revision": 2, "request_id": "x", "bogus": 1},
    )
    assert r.status_code == 422


# ---------------- 三方合并：保留后续其他路径的修改 ----------------


def test_revert_old_revision_keeps_later_disjoint_changes(client):
    _create(client, {"a": 1, "b": 2})
    # rev2 改 a（待撤销目标）
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    # rev3 在其他路径改 b
    assert _patch(client, "cfg", 2, "r3", {"b": 20}).status_code == 200
    # 撤销 rev2：a 回到 1，rev3 对 b 的修改保留
    r = _revert(client, "cfg", 2, "undo-r2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 4
    assert body["replayed"] is True
    assert body["content"] == {"a": 1, "b": 20}

    rev4 = client.get("/documents/cfg/revisions/4").json()
    assert rev4["base_revision"] == 2
    assert rev4["replayed"] is True


def test_revert_old_revision_keeps_later_nested_disjoint_changes(client):
    _create(client, {"db": {"host": "h", "port": 5432}})
    assert _patch(client, "cfg", 1, "r2", {"db": {"port": 6543}}).status_code == 200
    assert _patch(
        client, "cfg", 2, "r3", {"db": {"ssl": True}}
    ).status_code == 200
    r = _revert(client, "cfg", 2, "undo-port")
    assert r.status_code == 200
    assert r.json()["content"] == {
        "db": {"host": "h", "port": 5432, "ssl": True}
    }


def test_revert_then_later_revision_changed_same_path_same_value_ok(client):
    # 目标修订把 a 改成 10；后续修订把 a 改成 10（相同结果）→ 不算分歧
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 10}).status_code == 200
    r = _revert(client, "cfg", 2, "undo-r2")
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 1}


# ---------------- 三方冲突：409 revert_conflict，且不写入 ----------------


def test_revert_conflict_when_path_changed_differently_afterward(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 30}).status_code == 200
    before = client.get("/documents/cfg").json()

    r = _revert(client, "cfg", 2, "undo-r2")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "revert_conflict"
    assert detail["document_id"] == "cfg"
    assert detail["request_id"] == "undo-r2"
    assert detail["revert_revision"] == 2
    assert detail["base_revision"] == 2
    assert detail["current_revision"] == 3
    conflict = detail["conflicts"][0]
    assert conflict["path"] == "/a"
    # base=目标修订 rev2(a=10)，current=当前(a=30)，theirs=撤销想要的前态(a=1)
    assert conflict["values"] == {"base": 10, "current": 30, "theirs": 1}
    assert conflict["missing"] == []

    # 事务回滚：内容与修订号保持 rev3
    after = client.get("/documents/cfg").json()
    assert after == before
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3]


def test_revert_conflict_nested_path_is_json_pointer(client):
    _create(client, {"db": {"host": "h", "port": 5432}})
    assert _patch(
        client, "cfg", 1, "r2", {"db": {"port": 6543}}
    ).status_code == 200
    assert _patch(
        client, "cfg", 2, "r3", {"db": {"port": 3306}}
    ).status_code == 200
    r = _revert(client, "cfg", 2, "undo-r2")
    assert r.status_code == 409
    c = r.json()["detail"]["conflicts"][0]
    assert c["path"] == "/db/port"
    assert c["values"] == {"base": 6543, "current": 3306, "theirs": 5432}


def test_revert_conflict_does_not_consume_request_id(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 30}).status_code == 200
    # 冲突请求不占用 request_id：等待 a 不再分歧后，用同 ID 撤销只动 b 的 rev4
    r = _revert(client, "cfg", 2, "undo-retry")
    assert r.status_code == 409
    assert _patch(client, "cfg", 3, "r4", {"b": 22}).status_code == 200
    assert _patch(client, "cfg", 4, "r5", {"a": 10}).status_code == 200
    # 现在 current(a=10) == 目标 rev2(a=10)，撤销 rev4(b:2→22) 与 a 路径无关
    r2 = _revert(client, "cfg", 4, "undo-retry")
    assert r2.status_code == 200, r2.text
    assert r2.json()["content"] == {"a": 10, "b": 2}


def test_revert_delete_vs_later_edit_conflict(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": None}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 99}).status_code == 200
    r = _revert(client, "cfg", 2, "undo-del")
    assert r.status_code == 409
    c = r.json()["detail"]["conflicts"][0]
    assert c["path"] == "/a"
    # base(rev2 删除)=MISSING，theirs(撤销删除)=1，current=99
    assert c["values"] == {"base": None, "current": 99, "theirs": 1}
    assert c["missing"] == ["base"]


# ---------------- 无变化：不产生新修订 ----------------


def test_revert_noop_when_target_change_already_present_in_current(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    # rev3 只改其他路径；当前 a 与目标修订 rev2 一致，撤销 rev2 的逆向补丁
    # 三方重放后：a 的 theirs(1) 相对 base(10) 是改动，current(10)==base，取 1……
    # 因此该场景不是 no-op。真正的 no-op：当前内容已等于前态，
    # 且之后的修改把目标效果整体带回——这里用“目标修订本身加了 c，后来 c 被删”：
    assert _patch(client, "cfg", 2, "r3", {"c": 3}).status_code == 200
    assert _patch(client, "cfg", 3, "r4", {"c": None}).status_code == 200
    # 撤销 rev3（新增 c）：base(rev3 有 c)，current(rev4 无 c)，theirs(无 c)
    # t != b 而 c == t → 取 current（无 c），与当前版相同 → changed=false
    r = _revert(client, "cfg", 3, "undo-r3")
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["revision"] == 4
    assert body["content"] == {"a": 10, "b": 2}
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3, 4]


# ---------------- 幂等 ----------------


def test_revert_same_request_id_is_idempotent(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    r1 = _revert(client, "cfg", 2, "undo", author="alice")
    assert r1.status_code == 200
    first = r1.json()
    # 再发一次同样的撤销请求
    r2 = _revert(client, "cfg", 2, "undo", author="alice")
    assert r2.status_code == 200
    second = r2.json()
    assert second["idempotent"] is True
    assert second["revision"] == first["revision"] == 3
    assert second["content"] == first["content"]
    assert second["reverted_revision"] == 2
    # 没有额外修订
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2, 3]


def test_revert_same_request_id_different_revision_conflicts(client):
    _create(client, {"a": 1, "b": 2})
    assert _patch(client, "cfg", 1, "r2", {"a": 2}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"b": 3}).status_code == 200
    # 撤销 rev2：与 rev3 路径不相交，成功（a 回 1，b 保留 3）
    assert _revert(client, "cfg", 2, "undo").status_code == 200
    # 同 request_id 撤销不同修订 → 指纹不同 → 409
    r = _revert(client, "cfg", 3, "undo")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"


def test_revert_same_request_id_different_author_conflicts(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": 2}).status_code == 200
    assert _revert(client, "cfg", 2, "undo", author="alice").status_code == 200
    r = _revert(client, "cfg", 2, "undo", author="bob")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"


def test_revert_request_id_shared_with_patch_conflicts(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "shared", {"a": 2}).status_code == 200
    # 撤销用到了 PATCH 已占用的 request_id → 409（两种接口共用幂等表）
    r = _revert(client, "cfg", 2, "shared")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"
    # 反向同样成立
    assert _revert(client, "cfg", 2, "undo-2").status_code == 200
    r2 = _patch(client, "cfg", 3, "undo-2", {"a": 5})
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "request_id_conflict"


def test_idempotent_revert_replay_after_further_changes(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    first = _revert(client, "cfg", 2, "undo").json()
    # 撤销之后文档又前进；同 request_id 重放仍返回首次撤销的原始结果
    assert _patch(client, "cfg", 3, "r4", {"a": 77}).status_code == 200
    r = _revert(client, "cfg", 2, "undo")
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is True
    assert body["revision"] == first["revision"] == 3
    assert body["content"] == {"a": 1}


# ---------------- 契约校验 ----------------


SCHEMA = {
    "type": "object",
    "properties": {
        "a": {"type": "integer", "minimum": 0},
        "b": {"type": "string"},
    },
    "required": ["a"],
    "additionalProperties": False,
}


def test_revert_checked_against_current_contract(client):
    _create(client, {"a": 1, "b": "x"})
    # rev2 在无契约时新增 c（随后会删除，使当前文档满足严格契约）
    assert _patch(client, "cfg", 1, "r2", {"c": 3}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"c": None}).status_code == 200
    r = client.put("/documents/cfg/schema", json={"schema": SCHEMA})
    assert r.status_code == 200, r.text
    # 撤销 rev2（新增 c）：c 从当前版（已无 c）重放，候选仍无 c，满足契约 → 成功，
    # changed=false；契约没有阻挡一个合法候选
    ok = _revert(client, "cfg", 2, "undo-ok")
    assert ok.status_code == 200, ok.text
    assert ok.json()["changed"] is False
    assert ok.json()["content"] == {"a": 1, "b": "x"}


def test_revert_failing_contract_422_and_writes_nothing(client):
    # rev1: {"a": 10}；无契约时 rev2 新增 extra 键，rev3 再删掉 extra（当前合法），
    # 随后挂 additionalProperties:false 契约；撤销 rev3 会把 extra 加回来 → 422。
    _create(client, {"a": 10}, doc_id="cfg")
    assert _patch(client, "cfg", 1, "r2", {"extra": 1}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"extra": None}).status_code == 200
    strict = {
        "type": "object",
        "properties": {"a": {"type": "integer", "minimum": 10}},
        "required": ["a"],
        "additionalProperties": False,
    }
    assert client.put("/documents/cfg/schema", json={"schema": strict}).status_code == 200

    before = client.get("/documents/cfg").json()
    r = _revert(client, "cfg", 3, "undo-r3")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    # additionalProperties 错误挂在实例根路径 ""，消息中点名 extra
    assert any("extra" in e["message"] for e in detail["errors"])
    # 不写修订、不占用 request_id
    assert client.get("/documents/cfg").json() == before
    # 同一 request_id 改撤 rev2（撤销“新增 extra”）：候选与当前一致，changed=false
    r2 = _revert(client, "cfg", 2, "undo-r3")
    assert r2.status_code == 200, r2.text
    assert r2.json()["changed"] is False
    assert r2.json()["content"] == {"a": 10}


def test_revert_conflict_takes_precedence_over_contract(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "r2", {"a": 10}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 30}).status_code == 200
    # 严格契约（a 最小 0，撤销值 1 合法，当前 30 也合法）
    assert client.put(
        "/documents/cfg/schema",
        json={
            "schema": {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "additionalProperties": False,
            }
        },
    ).status_code == 200
    r = _revert(client, "cfg", 2, "undo-conflict")
    # 路径分歧优先于契约校验 → 409 revert_conflict
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "revert_conflict"


# ---------------- 历史与完整流程 ----------------


def test_revert_appears_as_normal_revision_in_history(client):
    _create(client, {"a": 1, "v": 0})
    assert _patch(client, "cfg", 1, "r2", {"v": 1}).status_code == 200
    assert _patch(client, "cfg", 2, "r3", {"a": 2}).status_code == 200
    # 撤销 rev2（v:0→1），rev3 只改了 a → v 回到 0，a 保留
    assert _revert(client, "cfg", 2, "undo-r2").status_code == 200
    items = client.get("/documents/cfg/revisions?order=desc").json()["items"]
    assert [h["revision"] for h in items] == [4, 3, 2, 1]
    latest = items[0]
    assert latest["request_id"] == "undo-r2"
    assert latest["base_revision"] == 2
    assert latest["author"] is None
    # 撤销本身是普通修订，还可以被再次撤销（“重做”）：
    # 撤销 rev4（v:1→0 的撤销）→ v 重新变为 1；a 路径三方合并保留当前值 2
    r = _revert(client, "cfg", 4, "undo-undo")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reverted_revision"] == 4
    # rev4 即当前版（其后无修订）→ 快路径
    assert body["replayed"] is False
    assert body["content"] == {"a": 2, "v": 1}
