"""GET /documents/{id}/blame 历史字段归因端到端集成测试（独立临时数据库）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 与 test_api 相同：直接改 settings.db_path，避免模块单例身份漂移
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
    body = {
        "base_revision": base_revision,
        "request_id": request_id,
        "patch": patch,
    }
    if author is not None:
        body["author"] = author
    r = client.patch(f"/documents/{doc_id}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _blame(client, revision, doc_id="cfg"):
    return client.get(f"/documents/{doc_id}/blame", params={"revision": revision})


def _by_path(body):
    return {entry["path"]: entry for entry in body["entries"]}


# ---------------- 基本归因 ----------------


def test_single_revision_attributes_all_leaves_to_creation(client):
    created = _create(client, {"a": 1, "b": {"x": 2}}, author="alice")
    r = _blame(client, 1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == "cfg"
    assert body["revision"] == 1
    assert body["entries"] == [
        {
            "path": "/a",
            "value": 1,
            "last_changed_revision": 1,
            "author": "alice",
            "request_id": None,
            "created_at": created["created_at"],
        },
        {
            "path": "/b/x",
            "value": 2,
            "last_changed_revision": 1,
            "author": "alice",
            "request_id": None,
            "created_at": created["created_at"],
        },
    ]


def test_changed_leaf_gets_new_revision_unchanged_keeps_original(client):
    _create(client, {"a": 1, "b": 2}, author="alice")
    _patch(client, "cfg", 1, "r1", {"a": 100}, author="bob")
    body = _blame(client, 2).json()
    entries = _by_path(body)
    assert entries["/a"]["last_changed_revision"] == 2
    assert entries["/a"]["author"] == "bob"
    assert entries["/a"]["request_id"] == "r1"
    assert entries["/a"]["value"] == 100
    # 未变化的叶子沿用修订 1 的归属
    assert entries["/b"]["last_changed_revision"] == 1
    assert entries["/b"]["author"] == "alice"
    assert entries["/b"]["request_id"] is None


def test_entry_metadata_matches_revision_record(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "req-xyz", {"a": 2}, author="carol")
    rev2 = client.get("/documents/cfg/revisions/2").json()
    body = _blame(client, 2).json()
    (entry,) = body["entries"]
    assert entry["last_changed_revision"] == 2
    assert entry["author"] == rev2["author"] == "carol"
    assert entry["request_id"] == rev2["request_id"] == "req-xyz"
    assert entry["created_at"] == rev2["created_at"]


def test_blame_targets_historical_revision_not_current(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 10})
    _patch(client, "cfg", 2, "r2", {"a": 99, "b": 20})
    # 以修订 2 为目标快照：/a 归属修订 2（值 10），/b 归属修订 1（值 2）
    body = _blame(client, 2).json()
    assert body["revision"] == 2
    entries = _by_path(body)
    assert entries["/a"]["value"] == 10
    assert entries["/a"]["last_changed_revision"] == 2
    assert entries["/b"]["value"] == 2
    assert entries["/b"]["last_changed_revision"] == 1


def test_nested_objects_recurse_to_leaves(client):
    _create(client, {"c": {"x": 1, "y": 2}, "top": 0})
    _patch(client, "cfg", 1, "r1", {"c": {"x": 9}})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    assert entries["/c/x"]["last_changed_revision"] == 2
    assert entries["/c/x"]["value"] == 9
    assert entries["/c/y"]["last_changed_revision"] == 1
    assert entries["/top"]["last_changed_revision"] == 1


def test_new_leaf_attributes_to_introducing_revision(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "r1", {"b": {"c": 5}})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    assert entries["/b/c"]["last_changed_revision"] == 2
    assert entries["/a"]["last_changed_revision"] == 1


# ---------------- 叶子语义：数组 / 空对象 / null / 根 ----------------


def test_arrays_are_whole_leaves(client):
    _create(client, {"arr": [1, 2], "nested": {"list": ["a"]}})
    _patch(client, "cfg", 1, "r1", {"arr": [1, 2, 3]})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    # 数组整体视为叶子：不展开下标，整个数组路径归属于修改它的修订
    assert entries["/arr"] == {
        "path": "/arr",
        "value": [1, 2, 3],
        "last_changed_revision": 2,
        "author": None,
        "request_id": "r1",
        "created_at": entries["/arr"]["created_at"],
    }
    assert "/arr/0" not in entries
    assert entries["/nested/list"]["value"] == ["a"]
    assert entries["/nested/list"]["last_changed_revision"] == 1


def test_empty_object_is_a_leaf(client):
    _create(client, {"a": {}, "b": 1})
    body = _blame(client, 1).json()
    entries = _by_path(body)
    assert entries["/a"]["value"] == {}
    assert entries["/a"]["last_changed_revision"] == 1
    # 空对象填入内容后，子路径归属于填入的修订
    _patch(client, "cfg", 1, "r1", {"a": {"x": 1}})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    assert "/a" not in entries
    assert entries["/a/x"]["last_changed_revision"] == 2


def test_real_null_is_a_leaf_value(client):
    _create(client, {"note": None, "a": 1})
    _patch(client, "cfg", 1, "r1", {"a": 2})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    # 真实存储的 null 是叶子值，归属不受其他路径修改影响
    assert entries["/note"]["value"] is None
    assert entries["/note"]["last_changed_revision"] == 1


def test_root_scalar_document_is_single_leaf(client):
    _create(client, 5)
    body = _blame(client, 1).json()
    assert [e["path"] for e in body["entries"]] == [""]
    assert body["entries"][0]["value"] == 5
    assert body["entries"][0]["last_changed_revision"] == 1
    # 非对象 patch 整体替换后，根叶子归属于新修订
    _patch(client, "cfg", 1, "r1", [1, 2])
    body = _blame(client, 2).json()
    assert body["entries"][0]["path"] == ""
    assert body["entries"][0]["value"] == [1, 2]
    assert body["entries"][0]["last_changed_revision"] == 2


def test_object_to_scalar_and_back_reattributes(client):
    _create(client, {"a": {"x": 1}})
    _patch(client, "cfg", 1, "r1", {"a": 5})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    # 对象被标量整体替换：/a 成为叶子并归属于修订 2
    assert entries["/a"]["value"] == 5
    assert entries["/a"]["last_changed_revision"] == 2
    assert "/a/x" not in entries
    # 再变回同内容对象：/a/x 视为重建，归属于修订 3 而非修订 1
    _patch(client, "cfg", 2, "r2", {"a": {"x": 1}})
    body = _blame(client, 3).json()
    entries = _by_path(body)
    assert entries["/a/x"]["value"] == 1
    assert entries["/a/x"]["last_changed_revision"] == 3


# ---------------- 删除后重建：归属于重建修订 ----------------


def test_delete_and_recreate_same_value_attributes_to_recreator(client):
    _create(client, {"a": 1, "b": 2}, author="alice")
    _patch(client, "cfg", 1, "r1", {"b": None})  # 删除 b
    body = _blame(client, 2).json()
    assert "/b" not in _by_path(body)
    _patch(client, "cfg", 2, "r2", {"b": 2}, author="bob")  # 以相同值重建 b
    body = _blame(client, 3).json()
    entries = _by_path(body)
    # 值与删除前相同，仍归属于重建它的修订 3
    assert entries["/b"]["value"] == 2
    assert entries["/b"]["last_changed_revision"] == 3
    assert entries["/b"]["author"] == "bob"
    assert entries["/b"]["request_id"] == "r2"
    assert entries["/a"]["last_changed_revision"] == 1


def test_delete_and_recreate_different_value_attributes_to_recreator(client):
    _create(client, {"b": 2})
    _patch(client, "cfg", 1, "r1", {"b": None})
    _patch(client, "cfg", 2, "r2", {"b": 7})
    body = _blame(client, 3).json()
    entries = _by_path(body)
    assert entries["/b"]["value"] == 7
    assert entries["/b"]["last_changed_revision"] == 3


def test_subtree_delete_and_recreate_reattributes_all_leaves(client):
    _create(client, {"s": {"x": 1, "y": 2}, "keep": 0})
    _patch(client, "cfg", 1, "r1", {"s": None})
    _patch(client, "cfg", 2, "r2", {"s": {"x": 1, "y": 2}})
    body = _blame(client, 3).json()
    entries = _by_path(body)
    # 整个子树按原样重建：所有子叶子都归属于重建修订
    assert entries["/s/x"]["last_changed_revision"] == 3
    assert entries["/s/y"]["last_changed_revision"] == 3
    assert entries["/keep"]["last_changed_revision"] == 1


# ---------------- 值比较 / 排序 / 转义 ----------------


def test_number_type_change_reattributes(client):
    # 1 → 1.0 / 1 → true：Python == 判等但落库字节不同，视为发生变化
    _create(client, {"i": 1, "b": 1})
    _patch(client, "cfg", 1, "r1", {"i": 1.0, "b": True})
    body = _blame(client, 2).json()
    entries = _by_path(body)
    assert entries["/i"]["value"] == 1.0
    assert entries["/i"]["last_changed_revision"] == 2
    assert entries["/b"]["value"] is True
    assert entries["/b"]["last_changed_revision"] == 2


def test_entries_sorted_by_path(client):
    _create(client, {"m": {"n": 1}, "c": 1, "a": 1})
    _patch(client, "cfg", 1, "r1", {"c": 2})
    body = _blame(client, 2).json()
    assert [e["path"] for e in body["entries"]] == ["/a", "/c", "/m/n"]


def test_paths_escape_special_keys_per_rfc6901(client):
    _create(client, {"a/b": 1, "t~k": 2})
    body = _blame(client, 1).json()
    assert [e["path"] for e in body["entries"]] == ["/a~1b", "/t~0k"]


# ---------------- 跨修订来源（重放 / 撤销 / 发布） ----------------


def test_replayed_revision_attributes_by_result_content(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 10})
    # 过期基线 + 不冲突路径 → 自动重放产生修订 3
    replayed = _patch(client, "cfg", 1, "r2", {"b": 20})
    assert replayed["replayed"] is True
    body = _blame(client, 3).json()
    entries = _by_path(body)
    assert entries["/a"]["last_changed_revision"] == 2
    assert entries["/b"]["last_changed_revision"] == 3
    assert entries["/b"]["request_id"] == "r2"


def test_revert_produced_revision_is_attributed(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 100})
    r = client.post(
        "/documents/cfg/reverts",
        json={"revision": 2, "request_id": "undo-1", "author": "alice"},
    )
    assert r.status_code == 200, r.text
    body = _blame(client, 3).json()
    entries = _by_path(body)
    # 撤销把 /a 改回 1：归属于撤销产生的修订 3
    assert entries["/a"]["value"] == 1
    assert entries["/a"]["last_changed_revision"] == 3
    assert entries["/a"]["author"] == "alice"
    assert entries["/a"]["request_id"] == "undo-1"
    assert entries["/b"]["last_changed_revision"] == 1


def test_release_produced_revision_carries_release_id(client):
    _create(client, {"a": 1})
    _create(client, {"b": 1}, doc_id="other")
    r = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "updates": [
                {"document_id": "cfg", "base_revision": 1, "patch": {"a": 2}},
                {"document_id": "other", "base_revision": 1, "patch": {"b": 2}},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = _blame(client, 2).json()
    entries = _by_path(body)
    # 发布产生的修订以 release_id 作为 request_id
    assert entries["/a"]["last_changed_revision"] == 2
    assert entries["/a"]["request_id"] == "rel-1"


# ---------------- 只读保证 ----------------


def test_blame_does_not_write_and_is_repeatable(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 10})
    before_doc = client.get("/documents/cfg").json()
    before_revisions = client.get("/documents/cfg/revisions").json()["items"]

    first = _blame(client, 2)
    assert first.status_code == 200, first.text
    second = _blame(client, 2)
    # 重复查询结果一致
    assert second.json() == first.json()
    # 文档与修订历史不因查询产生任何变化
    assert client.get("/documents/cfg").json() == before_doc
    assert client.get("/documents/cfg/revisions").json()["items"] == before_revisions


# ---------------- 404 与参数校验 ----------------


def test_missing_document_returns_404(client):
    r = _blame(client, 1, doc_id="ghost")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_missing_revision_returns_404(client):
    _create(client, {"a": 1})
    r = _blame(client, 99)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["document_id"] == "cfg"
    assert detail["revision"] == 99


def test_revision_param_required_and_positive(client):
    _create(client, {"a": 1})
    assert client.get("/documents/cfg/blame").status_code == 422
    assert _blame(client, 0).status_code == 422
    assert _blame(client, -1).status_code == 422
    assert client.get("/documents/cfg/blame?revision=x").status_code == 422
