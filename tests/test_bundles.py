"""POST /bundles/lock 跨文档依赖锁定包集成测试（独立临时数据库）。

锁定包为只读分析：在同一读事务快照内把 @current 固定为数字修订，递归收集
（文档, 修订）闭包；返回稳定排序的根 / 快照 / 边与 SHA-256 bundle_digest；
缺失文档 / 修订 / 指针返回 422 及完整引用链，且不产生写入。
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import canonical
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


def _lock(client, roots, **extra):
    body = {"roots": roots}
    body.update(extra)
    return client.post("/bundles/lock", json=body)


def _revision_count(client, doc_id):
    r = client.get(f"/documents/{doc_id}/revisions", params={"limit": 200})
    assert r.status_code == 200, r.text
    return len(r.json()["items"])


# ---------------- 基本锁定 ----------------


def test_single_root_current_pinned_and_snapshot_returned(client):
    _create(client, {"v": {"$ref": "doc:dep@current#/x"}, "own": 1})
    _create(client, {"x": 42}, doc_id="dep")
    r = _lock(client, [{"document_id": "cfg", "revision": "current"}])
    assert r.status_code == 200, r.text
    body = r.json()
    # current 固定为快照内数字修订；改写后根列表只含入参根
    assert body["roots"] == [
        {"document_id": "cfg", "revision": 1},
    ]
    # 快照为原始内容（引用不展开），按文档、修订排序
    assert body["documents"] == [
        {"document_id": "cfg", "revision": 1,
         "content": {"v": {"$ref": "doc:dep@current#/x"}, "own": 1}},
        {"document_id": "dep", "revision": 1, "content": {"x": 42}},
    ]
    assert body["edges"] == [
        {
            "document_id": "cfg",
            "revision": 1,
            "at": "/v",
            "ref": "doc:dep@current#/x",
            "target_document_id": "dep",
            "target_revision": 1,
            "pointer": "/x",
            "locked_ref": "doc:dep@1#/x",
        }
    ]
    expected_digest = hashlib.sha256(
        canonical(
            {
                "roots": body["roots"],
                "documents": body["documents"],
                "edges": body["edges"],
            }
        ).encode("utf-8")
    ).hexdigest()
    assert body["bundle_digest"] == expected_digest


def test_bundle_digest_matches_payload_without_digest_field(client):
    _create(client, {"a": {"$ref": "doc:dep@1#"}}, doc_id="root")
    _create(client, {"z": 1}, doc_id="dep")
    body = _lock(client, [{"document_id": "root", "revision": 1}]).json()
    payload = {"roots": body["roots"], "documents": body["documents"], "edges": body["edges"]}
    assert body["bundle_digest"] == hashlib.sha256(
        canonical(payload).encode("utf-8")
    ).hexdigest()


def test_numeric_revisions_kept_unchanged(client):
    _create(client, {"x": "new"}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"x": "old"})
    # 历史 rev1 与当前 rev2 同时被引用：数字保持不变，两版快照都在包里
    _create(
        client,
        {
            "old": {"$ref": "doc:dep@1#/x"},
            "new": {"$ref": "doc:dep@current#/x"},
        },
    )
    body = _lock(client, [{"document_id": "cfg", "revision": 1}]).json()
    assert body["roots"] == [{"document_id": "cfg", "revision": 1}]
    assert [(d["document_id"], d["revision"]) for d in body["documents"]] == [
        ("cfg", 1),
        ("dep", 1),
        ("dep", 2),
    ]
    assert [(e["at"], e["target_revision"], e["locked_ref"]) for e in body["edges"]] == [
        ("/new", 2, "doc:dep@2#/x"),
        ("/old", 1, "doc:dep@1#/x"),
    ]


def test_recursive_dependency_collection_and_dedup(client):
    _create(client, {"v": 7}, doc_id="c")
    _create(client, {"inner": {"$ref": "doc:c@1#/v"}}, doc_id="b")
    _create(
        client,
        {
            "first": {"$ref": "doc:b@1#/inner"},
            "second": {"$ref": "doc:c@1#/v"},
        },
    )
    body = _lock(client, [{"document_id": "cfg", "revision": "current"}]).json()
    # c 经 b 递归收集到，又被根直接引用：按 文档+修订 去重
    assert [(d["document_id"], d["revision"]) for d in body["documents"]] == [
        ("b", 1),
        ("c", 1),
        ("cfg", 1),
    ]
    assert [(e["document_id"], e["at"], e["target_document_id"], e["pointer"])
            for e in body["edges"]] == [
        ("b", "/inner", "c", "/v"),
        ("cfg", "/first", "b", "/inner"),
        ("cfg", "/second", "c", "/v"),
    ]


def test_whole_document_ref_with_empty_pointer(client):
    _create(client, {"x": 1}, doc_id="dep")
    _create(client, {"$ref": "doc:dep@current#"})
    body = _lock(client, [{"document_id": "cfg", "revision": 1}]).json()
    edge = body["edges"][0]
    assert edge["pointer"] == ""
    assert edge["at"] == ""
    assert edge["locked_ref"] == "doc:dep@1#"


def test_ref_inside_array(client):
    _create(client, ["zero", "one"], doc_id="arr")
    _create(client, {"list": [1, {"$ref": "doc:arr@1#/1"}, 3]})
    body = _lock(client, [{"document_id": "cfg", "revision": 1}]).json()
    assert body["edges"][0]["at"] == "/list/1"
    assert body["edges"][0]["pointer"] == "/1"


def test_non_reference_ref_objects_are_ignored(client):
    content = {
        "extra": {"$ref": "doc:ghost@1#/a", "other": 1},
        "bad": {"$ref": "doc:no-at-sign"},
        "nested": {"x": [{"$ref": "https://x"}]},
    }
    _create(client, content)
    body = _lock(client, [{"document_id": "cfg", "revision": 1}]).json()
    assert body["edges"] == []
    assert body["documents"] == [
        {"document_id": "cfg", "revision": 1, "content": content}
    ]


def test_multiple_roots_and_root_dedup(client):
    _create(client, {"x": 1}, doc_id="dep")
    _create(client, {"a": {"$ref": "doc:dep@1#/x"}}, doc_id="r1")
    _create(client, {"b": {"$ref": "doc:dep@1#/x"}}, doc_id="r2")
    # 重复根（含 current 与数字指向同一修订）只保留一次
    body = _lock(
        client,
        [
            {"document_id": "r2", "revision": "current"},
            {"document_id": "r1", "revision": 1},
            {"document_id": "r2", "revision": 1},
        ],
    ).json()
    # 改写后根列表只含入参根（重复根去重、按文档排序）
    assert body["roots"] == [
        {"document_id": "r1", "revision": 1},
        {"document_id": "r2", "revision": 1},
    ]
    # 源位置不同 → 两条边；目标 文档+修订+指针 相同但边不合并
    assert [(e["document_id"], e["at"]) for e in body["edges"]] == [
        ("r1", "/a"),
        ("r2", "/b"),
    ]


def test_roots_sorted_by_document_and_revision(client):
    _create(client, {}, doc_id="z")
    _create(client, {}, doc_id="a")
    _patch(client, "a", 1, "p1", {"x": 1})
    body = _lock(
        client,
        [{"document_id": "z", "revision": 1},
         {"document_id": "a", "revision": 2},
         {"document_id": "a", "revision": 1}],
    ).json()
    assert body["roots"] == [
        {"document_id": "a", "revision": 1},
        {"document_id": "a", "revision": 2},
        {"document_id": "z", "revision": 1},
    ]


# ---------------- 摘要：顺序无关 ----------------


def test_digest_invariant_to_root_order(client):
    _create(client, {"v": 7}, doc_id="c")
    _create(client, {"inner": {"$ref": "doc:c@current#/v"}}, doc_id="b")
    _create(client, {"first": {"$ref": "doc:b@1#/inner"}}, doc_id="a")
    roots_ab = [
        {"document_id": "a", "revision": "current"},
        {"document_id": "b", "revision": 1},
    ]
    roots_ba = [
        {"document_id": "b", "revision": 1},
        {"document_id": "a", "revision": "current"},
    ]
    body_ab = _lock(client, roots_ab).json()
    body_ba = _lock(client, roots_ba).json()
    assert body_ab["bundle_digest"] == body_ba["bundle_digest"]
    assert body_ab["roots"] == body_ba["roots"]
    assert body_ab["documents"] == body_ba["documents"]
    assert body_ab["edges"] == body_ba["edges"]


def test_digest_changes_when_closure_changes(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@current#/v"}})
    before = _lock(client, [{"document_id": "cfg", "revision": "current"}]).json()
    # 依赖前进：@current 闭包变为 dep@2
    _patch(client, "dep", 1, "p1", {"v": 2})
    after = _lock(client, [{"document_id": "cfg", "revision": "current"}]).json()
    assert before["bundle_digest"] != after["bundle_digest"]
    assert after["edges"][0]["target_revision"] == 2


# ---------------- 依赖成环不是错误 ----------------


def test_cycle_is_collected_not_an_error(client):
    _create(client, {"to_b": {"$ref": "doc:b@1#/x"}}, doc_id="a")
    _create(client, {"x": {"to_a": {"$ref": "doc:a@1#/to_b"}}}, doc_id="b")
    r = _lock(client, [{"document_id": "a", "revision": 1}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(d["document_id"], d["revision"]) for d in body["documents"]] == [
        ("a", 1),
        ("b", 1),
    ]
    # 环上两条边都记录，没有重复遍历
    assert [(e["document_id"], e["target_document_id"]) for e in body["edges"]] == [
        ("a", "b"),
        ("b", "a"),
    ]


def test_self_ref_is_collected_not_an_error(client):
    _create(client, {"x": 1, "self": {"$ref": "doc:cfg@1#/x"}})
    body = _lock(client, [{"document_id": "cfg", "revision": 1}]).json()
    assert len(body["edges"]) == 1
    assert body["edges"][0]["locked_ref"] == "doc:cfg@1#/x"
    assert [(d["document_id"], d["revision"]) for d in body["documents"]] == [("cfg", 1)]


# ---------------- 422：文档 / 修订 / 指针 ----------------


def test_missing_root_document_returns_422_with_chain(client):
    r = _lock(client, [{"document_id": "ghost", "revision": "current"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {"document_id": "ghost", "revision": None, "pointer": "",
         "ref": None, "at": None}
    ]


def test_missing_root_revision_returns_422(client):
    _create(client, {"a": 1})
    r = _lock(client, [{"document_id": "cfg", "revision": 9}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["revision"] == 9
    assert detail["current_revision"] == 1
    assert detail["chain"] == [
        {"document_id": "cfg", "revision": 9, "pointer": "", "ref": None, "at": None}
    ]


def test_missing_referenced_document_returns_422_with_full_chain(client):
    _create(client, {"v": {"$ref": "doc:ghost@current#/a"}})
    r = _lock(client, [{"document_id": "cfg", "revision": 1}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {"document_id": "cfg", "revision": 1, "pointer": "", "ref": None, "at": None},
        {"document_id": "ghost", "revision": None, "pointer": "/a",
         "ref": "doc:ghost@current#/a", "at": "/v"},
    ]


def test_missing_nested_document_chain_spans_multiple_hops(client):
    _create(client, {"x": 1}, doc_id="c")
    _create(client, {"via": {"$ref": "doc:c@1#/missing"}}, doc_id="b")
    _create(client, {"top": {"$ref": "doc:b@1#/via"}})
    r = _lock(client, [{"document_id": "cfg", "revision": 1}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "pointer_not_found"
    assert [(h["document_id"], h["pointer"]) for h in detail["chain"]] == [
        ("cfg", ""),
        ("b", "/via"),
        ("c", "/missing"),
    ]
    assert detail["chain"][1]["at"] == "/top"
    assert detail["chain"][2]["at"] == "/via"


def test_missing_revision_returns_422_with_chain(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@9#/a"}})
    r = _lock(client, [{"document_id": "cfg", "revision": 1}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["revision"] == 9
    assert detail["current_revision"] == 1
    assert detail["chain"][-1] == {
        "document_id": "dep",
        "revision": 9,
        "pointer": "/a",
        "ref": "doc:dep@9#/a",
        "at": "/v",
    }


def test_missing_pointer_returns_422_with_chain(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/nope"}})
    r = _lock(client, [{"document_id": "cfg", "revision": 1}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "pointer_not_found"
    assert detail["pointer"] == "/nope"
    assert [h["document_id"] for h in detail["chain"]] == ["cfg", "dep"]


def test_bad_pointer_escape_returns_422(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a~2b"}})
    r = _lock(client, [{"document_id": "cfg", "revision": 1}])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pointer_not_found"


def test_failure_in_second_root_still_422(client):
    _create(client, {"ok": 1}, doc_id="good")
    _create(client, {"v": {"$ref": "doc:ghost@1#/x"}}, doc_id="bad")
    r = _lock(
        client,
        [{"document_id": "good", "revision": 1},
         {"document_id": "bad", "revision": 1}],
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    # 链从失败的根开始
    assert [h["document_id"] for h in detail["chain"]] == ["bad", "ghost"]


# ---------------- 只读与请求校验 ----------------


def test_lock_is_read_only_on_failure(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/nope"}})
    before = client.get("/documents/cfg").json()
    assert _lock(client, [{"document_id": "cfg", "revision": 1}]).status_code == 422
    after = client.get("/documents/cfg").json()
    assert after == before
    assert _revision_count(client, "cfg") == 1
    assert _revision_count(client, "dep") == 1


def test_lock_success_is_read_only(client):
    _create(client, {"x": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/x"}})
    assert _lock(client, [{"document_id": "cfg", "revision": 1}]).status_code == 200
    assert client.get("/documents/cfg").json()["content"] == {
        "v": {"$ref": "doc:dep@1#/x"}
    }
    assert _revision_count(client, "cfg") == 1


def test_roots_count_validation(client):
    r = _lock(client, [])
    assert r.status_code == 422
    too_many = [{"document_id": f"d{i:02d}", "revision": 1} for i in range(21)]
    assert _lock(client, too_many).status_code == 422


def test_invalid_root_revision_values_rejected(client):
    for bad in (0, -1, "1", 1.5, True, "CURRENT", None):
        r = _lock(client, [{"document_id": "cfg", "revision": bad}])
        assert r.status_code == 422, bad


def test_unknown_field_rejected(client):
    r = client.post(
        "/bundles/lock",
        json={"roots": [{"document_id": "cfg", "revision": 1}], "bogus": 1},
    )
    assert r.status_code == 422


def test_root_id_pattern_rejected(client):
    r = _lock(client, [{"document_id": "bad id", "revision": 1}])
    assert r.status_code == 422
