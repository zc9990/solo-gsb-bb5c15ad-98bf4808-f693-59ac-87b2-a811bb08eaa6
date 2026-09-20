"""GET /documents/{id}/diff 修订差异审计端到端集成测试（独立临时数据库）。"""
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


def _create(client, content=None, doc_id="cfg"):
    body = {"id": doc_id}
    if content is not None:
        body["content"] = content
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch):
    r = client.patch(
        f"/documents/{doc_id}",
        json={
            "base_revision": base_revision,
            "request_id": request_id,
            "patch": patch,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _diff(client, from_revision, to_revision, doc_id="cfg"):
    return client.get(
        f"/documents/{doc_id}/diff",
        params={"from_revision": from_revision, "to_revision": to_revision},
    )


# ---------------- 基本变更类型 ----------------


def test_replace_reports_before_after_and_counts(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 100})
    r = _diff(client, 1, 2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["document_id"] == "cfg"
    assert body["from_revision"] == 1
    assert body["to_revision"] == 2
    assert body["changes"] == [
        {"op": "replace", "path": "/a", "before": 1, "after": 100, "missing": []}
    ]
    assert body["counts"] == {"add": 0, "remove": 0, "replace": 1, "total": 1}


def test_add_and_remove_mark_missing_side(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"b": None, "c": {"x": 1}})
    body = _diff(client, 1, 2).json()
    # 按路径稳定排序：/b 在 /c 前
    assert body["changes"] == [
        {
            "op": "remove",
            "path": "/b",
            "before": 2,
            "after": None,
            "missing": ["after"],
        },
        {
            "op": "add",
            "path": "/c",
            "before": None,
            "after": {"x": 1},
            "missing": ["before"],
        },
    ]
    assert body["counts"] == {"add": 1, "remove": 1, "replace": 0, "total": 2}


def test_real_null_is_not_missing(client):
    # 真实存储的 null：before 为 null 但 missing 为空，与“键缺失”区分
    _create(client, {"note": None, "a": 1})
    _patch(client, "cfg", 1, "r1", {"note": "hi", "a": 2})
    body = _diff(client, 1, 2).json()
    by_path = {c["path"]: c for c in body["changes"]}
    assert by_path["/note"] == {
        "op": "replace",
        "path": "/note",
        "before": None,
        "after": "hi",
        "missing": [],
    }
    assert by_path["/a"]["missing"] == []
    assert body["counts"]["replace"] == 2


def test_whole_document_replaced_by_null(client):
    # after 为真实 null（整体替换为 null），missing 仍为空
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "r1", None)
    body = _diff(client, 1, 2).json()
    assert body["changes"] == [
        {"op": "replace", "path": "", "before": {"a": 1}, "after": None, "missing": []}
    ]


# ---------------- 递归与整体值语义 ----------------


def test_arrays_compared_as_whole_values(client):
    _create(client, {"arr": [1, 2], "nested": {"list": ["a"]}})
    _patch(client, "cfg", 1, "r1", {"arr": [1, 2, 3], "nested": {"list": ["b"]}})
    body = _diff(client, 1, 2).json()
    # 数组整体替换：只有 /arr 与 /nested/list 两条，不按下标展开
    assert body["changes"] == [
        {
            "op": "replace",
            "path": "/arr",
            "before": [1, 2],
            "after": [1, 2, 3],
            "missing": [],
        },
        {
            "op": "replace",
            "path": "/nested/list",
            "before": ["a"],
            "after": ["b"],
            "missing": [],
        },
    ]
    assert body["counts"] == {"add": 0, "remove": 0, "replace": 2, "total": 2}


def test_nested_recursion_sorted_by_pointer(client):
    _create(client, {"m": {"n": {"p": 1}}, "c": {"x": 1}, "a": 1})
    _patch(client, "cfg", 1, "r1", {"m": {"n": {"p": 2}}, "c": {"y": 2}, "a": 9})
    body = _diff(client, 1, 2).json()
    assert [c["path"] for c in body["changes"]] == ["/a", "/c/y", "/m/n/p"]
    assert body["counts"] == {"add": 1, "remove": 0, "replace": 2, "total": 3}


def test_subtree_add_remove_reported_as_whole_value(client):
    _create(client, {"gone": {"x": 1, "y": {"z": 2}}, "keep": 0})
    _patch(client, "cfg", 1, "r1", {"gone": None, "new": {"x": 1, "y": {"z": 2}}})
    body = _diff(client, 1, 2).json()
    # 新增 / 删除的子树作为整体值挂在该路径，不向下展开
    assert body["changes"] == [
        {
            "op": "remove",
            "path": "/gone",
            "before": {"x": 1, "y": {"z": 2}},
            "after": None,
            "missing": ["after"],
        },
        {
            "op": "add",
            "path": "/new",
            "before": None,
            "after": {"x": 1, "y": {"z": 2}},
            "missing": ["before"],
        },
    ]


def test_root_replace_when_document_type_changes(client):
    _create(client, {"a": {"b": 1}})
    _patch(client, "cfg", 1, "r1", [1, 2])
    body = _diff(client, 1, 2).json()
    assert body["changes"] == [
        {
            "op": "replace",
            "path": "",
            "before": {"a": {"b": 1}},
            "after": [1, 2],
            "missing": [],
        }
    ]


def test_number_type_change_is_reported(client):
    # 1 → 1.0 / 1 → true：Python == 判等但落库字节不同，审计必须报出
    _create(client, {"i": 1, "b": 1})
    _patch(client, "cfg", 1, "r1", {"i": 1.0, "b": True})
    body = _diff(client, 1, 2).json()
    assert [c["path"] for c in body["changes"]] == ["/b", "/i"]
    assert all(c["op"] == "replace" for c in body["changes"])


# ---------------- 空差异 / 方向 / 跨多修订 ----------------


def test_empty_diff_returns_empty_array_and_zero_counts(client):
    _create(client, {"a": 1, "b": {"c": [1, 2]}})
    body = _diff(client, 1, 1).json()
    assert body["changes"] == []
    assert body["counts"] == {"add": 0, "remove": 0, "replace": 0, "total": 0}


def test_diff_spans_multiple_revisions(client):
    _create(client, {"a": 1, "b": 2})
    _patch(client, "cfg", 1, "r1", {"a": 10})
    _patch(client, "cfg", 2, "r2", {"b": 20, "c": 30})
    body = _diff(client, 1, 3).json()
    assert body["changes"] == [
        {"op": "replace", "path": "/a", "before": 1, "after": 10, "missing": []},
        {"op": "replace", "path": "/b", "before": 2, "after": 20, "missing": []},
        {"op": "add", "path": "/c", "before": None, "after": 30, "missing": ["before"]},
    ]
    assert body["counts"] == {"add": 1, "remove": 0, "replace": 2, "total": 3}


def test_reverse_direction_swaps_before_after_and_ops(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "r1", {"a": 2, "b": 5})
    body = _diff(client, 2, 1).json()
    assert body["changes"] == [
        {"op": "replace", "path": "/a", "before": 2, "after": 1, "missing": []},
        {"op": "remove", "path": "/b", "before": 5, "after": None, "missing": ["after"]},
    ]


def test_paths_escape_special_keys_per_rfc6901(client):
    _create(client, {"a/b": 1, "t~k": 1})
    _patch(client, "cfg", 1, "r1", {"a/b": 2, "t~k": 2})
    body = _diff(client, 1, 2).json()
    assert [c["path"] for c in body["changes"]] == ["/a~1b", "/t~0k"]


# ---------------- 404 与参数校验 ----------------


def test_missing_document_returns_404(client):
    r = _diff(client, 1, 2, doc_id="ghost")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


def test_missing_revision_returns_404_and_names_it(client):
    _create(client, {"a": 1})
    r = _diff(client, 1, 99)
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["document_id"] == "cfg"
    assert detail["from_revision"] == 1
    assert detail["to_revision"] == 99
    assert detail["missing_revisions"] == [99]


def test_both_revisions_missing_are_listed(client):
    _create(client, {"a": 1})
    r = _diff(client, 98, 99)
    assert r.status_code == 404
    assert r.json()["detail"]["missing_revisions"] == [98, 99]
    # from 缺失、to 存在时只指出缺失的一侧
    r2 = _diff(client, 98, 1)
    assert r2.json()["detail"]["missing_revisions"] == [98]


def test_query_params_required_and_positive(client):
    _create(client, {"a": 1})
    assert client.get("/documents/cfg/diff").status_code == 422
    assert client.get("/documents/cfg/diff?from_revision=1").status_code == 422
    assert _diff(client, 0, 1).status_code == 422
    assert _diff(client, 1, -1).status_code == 422
    r = client.get("/documents/cfg/diff?from_revision=x&to_revision=1")
    assert r.status_code == 422
