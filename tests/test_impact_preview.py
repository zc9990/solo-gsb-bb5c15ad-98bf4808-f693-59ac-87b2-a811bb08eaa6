"""POST /documents/{id}/impact-preview 变更影响预演集成测试（独立临时数据库）。

预演为只读分析：在同一读事务快照内生成不落库候选，再分别以当前内容和
候选覆盖目标文档解析每个根文档的 $ref；不创建修订、不占用 request_id、
不改变租约。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 与其他集成测试相同：直接改 settings.db_path，避免模块单例身份漂移
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
    return client.patch(
        f"/documents/{doc_id}",
        json={
            "base_revision": base_revision,
            "request_id": request_id,
            "patch": patch,
        },
    )


def _preview(client, doc_id, base_revision, patch, roots, **extra):
    body = {
        "base_revision": base_revision,
        "patch": patch,
        "root_document_ids": roots,
    }
    body.update(extra)
    return client.post(f"/documents/{doc_id}/impact-preview", json=body)


def _revision_count(client, doc_id):
    r = client.get(f"/documents/{doc_id}/revisions", params={"limit": 200})
    assert r.status_code == 200, r.text
    return len(r.json()["items"])


# ---------------- 基本预演 ----------------


def test_preview_basic_resolved_changes_and_dependencies(client):
    _create(client, {"host": "a", "port": 5432}, doc_id="dep")
    _create(client, {"db": {"$ref": "doc:dep@current#"}, "debug": False}, doc_id="app")

    r = _preview(client, "dep", 1, {"host": "b"}, ["app"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "dep"
    assert body["base_revision"] == 1
    assert body["current_revision"] == 1
    assert body["candidate_revision"] == 2
    assert body["replayed"] is False
    assert body["changed"] is True
    assert body["candidate"] == {"host": "b", "port": 5432}
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["root_document_id"] == "app"
    assert result["revision"] == 1
    # 依赖为两遍解析的并集：当前 rev1 + 候选（视为下一个修订 rev2）
    assert result["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": ""},
        {"document_id": "dep", "revision": 2, "pointer": ""},
    ]
    assert result["resolved_changes"] == [
        {"op": "replace", "path": "/db/host", "before": "a", "after": "b", "missing": []}
    ]


def test_preview_does_not_create_revision_or_touch_document(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")

    r = _preview(client, "dep", 1, {"v": 2}, ["app"])
    assert r.status_code == 200, r.text

    doc = client.get("/documents/dep").json()
    assert doc["revision"] == 1
    assert doc["content"] == {"v": 1}
    assert _revision_count(client, "dep") == 1


def test_preview_does_not_occupy_request_id(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")

    r = _preview(client, "dep", 1, {"v": 2}, ["app"])
    assert r.status_code == 200, r.text

    # 预演之后任意 request_id 都未被占用：正常 PATCH 成功且非幂等重放
    p = _patch(client, "dep", 1, "req-after-preview", {"v": 2})
    assert p.status_code == 200, p.text
    assert p.json()["idempotent"] is False
    assert p.json()["revision"] == 2
    # 同 ID 重放仍是首次写入的结果（预演没有污染幂等表）
    replay = _patch(client, "dep", 1, "req-after-preview", {"v": 2})
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True


def test_preview_does_not_check_or_change_lease(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")
    lease = client.post(
        "/documents/dep/leases", json={"holder": "worker-1", "ttl_seconds": 30}
    )
    assert lease.status_code == 201, lease.text
    token = lease.json()["lease_token"]

    # 文档存在他人未过期租约时预演仍可只读执行（不检查租约）
    r = _preview(client, "dep", 1, {"v": 2}, ["app"])
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True

    # 租约状态未被改变：holder / token 不变，写入门禁仍然生效
    current = client.get("/documents/dep/leases").json()
    assert current["holder"] == "worker-1"
    assert current["lease_token"] == token
    assert current["expired"] is False
    blocked = _patch(client, "dep", 1, "req-no-token", {"v": 9})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "lease_conflict"


def test_preview_root_is_target_document(client):
    _create(client, {"a": 1, "b": {"x": 1}}, doc_id="cfg")

    r = _preview(client, "cfg", 1, {"a": 2, "b": {"y": 3}}, ["cfg"])
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["root_document_id"] == "cfg"
    assert result["revision"] == 1
    assert result["dependencies"] == []
    assert result["resolved_changes"] == [
        {"op": "replace", "path": "/a", "before": 1, "after": 2, "missing": []},
        {"op": "add", "path": "/b/y", "before": None, "after": 3, "missing": ["before"]},
    ]


def test_preview_noop_patch_reports_changed_false(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")

    r = _preview(client, "dep", 1, {"v": 1}, ["app"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is False
    # 无变化时候选不占用新修订号
    assert body["candidate_revision"] == body["current_revision"] == 1
    result = body["results"][0]
    assert result["resolved_changes"] == []
    # 仅当前一遍解析：依赖中不会出现虚拟修订
    assert result["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": ""}
    ]


def test_preview_stale_base_replays_three_way_merge(client):
    _create(client, {"a": 1, "b": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")
    p = _patch(client, "dep", 1, "req-advance", {"a": 100})
    assert p.status_code == 200, p.text

    r = _preview(client, "dep", 1, {"b": 2}, ["app"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replayed"] is True
    assert body["changed"] is True
    assert body["current_revision"] == 2
    assert body["candidate_revision"] == 3
    assert body["candidate"] == {"a": 100, "b": 2}
    assert body["results"][0]["resolved_changes"] == [
        {"op": "replace", "path": "/r/b", "before": 1, "after": 2, "missing": []}
    ]


def test_preview_resolved_changes_sorted_with_diff_semantics(client):
    _create(client, {"a": 1, "b": 2, "c": {"x": 1}, "note": None}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")

    patch = {"a": 10, "b": None, "c": {"y": 2}, "note": "hi"}
    r = _preview(client, "dep", 1, patch, ["app"])
    assert r.status_code == 200, r.text
    changes = r.json()["results"][0]["resolved_changes"]
    # 与 GET /documents/{id}/diff 同一语义：按 RFC 6901 路径排序，
    # missing 区分键缺失与真实 null
    assert changes == [
        {"op": "replace", "path": "/r/a", "before": 1, "after": 10, "missing": []},
        {"op": "remove", "path": "/r/b", "before": 2, "after": None, "missing": ["after"]},
        {"op": "add", "path": "/r/c/y", "before": None, "after": 2, "missing": ["before"]},
        {"op": "replace", "path": "/r/note", "before": None, "after": "hi", "missing": []},
    ]


def test_preview_pinned_historical_ref_not_affected_by_candidate(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(
        client,
        {
            "pinned": {"$ref": "doc:dep@1#/v"},
            "cur": {"$ref": "doc:dep@current#/v"},
        },
        doc_id="app",
    )

    r = _preview(client, "dep", 1, {"v": 2}, ["app"])
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    # @1 固定引用始终读历史快照，只有 @current 随候选变化
    assert result["resolved_changes"] == [
        {"op": "replace", "path": "/cur", "before": 1, "after": 2, "missing": []}
    ]
    assert result["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": "/v"},
        {"document_id": "dep", "revision": 2, "pointer": "/v"},
    ]


def test_preview_patch_introducing_new_ref_shows_new_dependency(client):
    _create(client, {"v": 7}, doc_id="dep")
    _create(client, {"a": 1}, doc_id="cfg")

    patch = {"link": {"$ref": "doc:dep@1#/v"}}
    r = _preview(client, "cfg", 1, patch, ["cfg"])
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": "/v"}
    ]
    assert result["resolved_changes"] == [
        {"op": "add", "path": "/link", "before": None, "after": 7, "missing": ["before"]}
    ]


def test_preview_multiple_roots_in_input_order(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app1")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app2")

    r = _preview(client, "dep", 1, {"v": 2}, ["app2", "app1"])
    assert r.status_code == 200, r.text
    assert [item["root_document_id"] for item in r.json()["results"]] == [
        "app2",
        "app1",
    ]


def test_preview_duplicate_roots_deduped_by_first_occurrence(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")

    r = _preview(client, "dep", 1, {"v": 2}, ["app", "dep", "app", "dep", "app"])
    assert r.status_code == 200, r.text
    assert [item["root_document_id"] for item in r.json()["results"]] == ["app", "dep"]


# ---------------- 候选生成失败：整体结构化错误 ----------------


def test_preview_target_document_not_found(client):
    r = _preview(client, "ghost", 1, {"a": 1}, ["cfg"])
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"error": "not_found", "document_id": "ghost"}


def test_preview_unknown_base_revision(client):
    _create(client, {"v": 1}, doc_id="dep")
    r = _preview(client, "dep", 99, {"v": 2}, ["dep"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "unknown_base_revision"
    assert detail["base_revision"] == 99
    assert detail["current_revision"] == 1


def test_preview_conflict_returns_existing_structured_error(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="app")
    p = _patch(client, "dep", 1, "req-advance", {"a": 100})
    assert p.status_code == 200, p.text

    r = _preview(client, "dep", 1, {"a": 999}, ["app"])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    # 与 PATCH 相同的结构化冲突错误（预演无 request_id 字段）
    assert detail == {
        "error": "conflict",
        "document_id": "dep",
        "base_revision": 1,
        "current_revision": 2,
        "conflicts": [
            {
                "path": "/a",
                "values": {"base": 1, "current": 100, "theirs": 999},
                "missing": [],
            }
        ],
    }


def test_preview_contract_failure_returns_existing_structured_error(client):
    _create(client, {"a": 1}, doc_id="dep")
    r = client.put(
        "/documents/dep/schema",
        json={
            "schema": {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
                "required": ["a"],
            }
        },
    )
    assert r.status_code == 200, r.text

    r = _preview(client, "dep", 1, {"a": "oops"}, ["dep"])
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "schema_validation_failed"
    assert detail["document_id"] == "dep"
    assert "request_id" not in detail
    assert detail["errors"][0]["validator"] == "type"
    # 契约失败同样不落库
    assert client.get("/documents/dep").json()["revision"] == 1


# ---------------- 单根解析失败：隔离记录 ----------------


def test_preview_single_root_failure_isolated(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#"}}, doc_id="good")
    _create(client, {"r": {"$ref": "doc:ghost@current#"}}, doc_id="broken")

    r = _preview(client, "dep", 1, {"v": 2}, ["good", "broken", "dep"])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [item["root_document_id"] for item in results] == ["good", "broken", "dep"]

    ok = results[0]
    assert ok["resolved_changes"] == [
        {"op": "replace", "path": "/r/v", "before": 1, "after": 2, "missing": []}
    ]

    failed = results[1]
    assert failed["phase"] == "current"
    assert failed["error"] == "document_not_found"
    assert failed["status_code"] == 422
    assert failed["document_id"] == "ghost"
    # 完整引用链：从根文档出发到失败一跳
    assert failed["chain"] == [
        {"document_id": "broken", "revision": 1, "pointer": "", "ref": None, "at": None},
        {
            "document_id": "ghost",
            "revision": None,
            "pointer": "",
            "ref": "doc:ghost@current#",
            "at": "/r",
        },
    ]

    target_root = results[2]
    assert target_root["resolved_changes"] == [
        {"op": "replace", "path": "/v", "before": 1, "after": 2, "missing": []}
    ]


def test_preview_candidate_phase_failure(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"r": {"$ref": "doc:dep@current#/v"}}, doc_id="app")

    # 候选删除被引用的键：当前解析成功，候选解析失败
    r = _preview(client, "dep", 1, {"v": None}, ["app"])
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["root_document_id"] == "app"
    assert result["phase"] == "candidate"
    assert result["error"] == "pointer_not_found"
    assert result["status_code"] == 422
    assert result["chain"][0] == {
        "document_id": "app",
        "revision": 1,
        "pointer": "",
        "ref": None,
        "at": None,
    }
    assert result["chain"][1]["document_id"] == "dep"
    assert result["chain"][1]["revision"] == 2  # 候选解析固定到 candidate_revision
    assert result["chain"][1]["ref"] == "doc:dep@current#/v"


def test_preview_candidate_cycle_reported_per_root(client):
    _create(client, {"self": 1}, doc_id="cfg")

    # 候选把 self 改成对自身的 @current 引用 → 候选解析成环（409 类错误同样隔离记录）
    patch = {"self": {"$ref": "doc:cfg@current#"}}
    r = _preview(client, "cfg", 1, patch, ["cfg"])
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["phase"] == "candidate"
    assert result["error"] == "reference_cycle"
    assert result["status_code"] == 409
    # 环链首尾闭合
    chain = result["chain"]
    assert chain[0]["document_id"] == chain[-1]["document_id"] == "cfg"
    assert (chain[0]["revision"], chain[0]["pointer"]) == (
        chain[-1]["revision"],
        chain[-1]["pointer"],
    )


def test_preview_missing_root_document_recorded_per_root(client):
    _create(client, {"v": 1}, doc_id="dep")

    r = _preview(client, "dep", 1, {"v": 2}, ["ghost", "dep"])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["root_document_id"] == "ghost"
    assert results[0]["phase"] == "current"
    assert results[0]["error"] == "document_not_found"
    assert results[0]["chain"] == [
        {"document_id": "ghost", "revision": None, "pointer": "", "ref": None, "at": None}
    ]
    assert results[1]["root_document_id"] == "dep"
    assert "error" not in results[1]


# ---------------- 请求模型校验 ----------------


def test_preview_roots_count_bounds(client):
    _create(client, {"v": 1}, doc_id="dep")

    r = _preview(client, "dep", 1, {"v": 2}, [])
    assert r.status_code == 422

    r = _preview(client, "dep", 1, {"v": 2}, [f"root{i}" for i in range(21)])
    assert r.status_code == 422

    r = _preview(client, "dep", 1, {"v": 2}, [f"root{i}" for i in range(20)])
    assert r.status_code == 200, r.text


def test_preview_rejects_extra_fields(client):
    _create(client, {"v": 1}, doc_id="dep")

    r = _preview(client, "dep", 1, {"v": 2}, ["dep"], request_id="req-x")
    assert r.status_code == 422
    r = _preview(client, "dep", 1, {"v": 2}, ["dep"], author="alice")
    assert r.status_code == 422
    r = _preview(client, "dep", 1, {"v": 2}, ["dep"], guards=[])
    assert r.status_code == 422
    r = _preview(client, "dep", 1, {"v": 2}, ["dep"], lease_token=1)
    assert r.status_code == 422


def test_preview_missing_required_fields(client):
    _create(client, {"v": 1}, doc_id="dep")
    r = client.post("/documents/dep/impact-preview", json={"patch": {"v": 2}})
    assert r.status_code == 422
    r = client.post(
        "/documents/dep/impact-preview",
        json={"base_revision": 1, "root_document_ids": ["dep"]},
    )
    assert r.status_code == 422


def test_preview_base_revision_must_be_positive(client):
    _create(client, {"v": 1}, doc_id="dep")
    r = _preview(client, "dep", 0, {"v": 2}, ["dep"])
    assert r.status_code == 422
