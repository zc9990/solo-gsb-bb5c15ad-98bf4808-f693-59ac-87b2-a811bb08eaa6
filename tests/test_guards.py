"""guards 更新前置条件端到端集成测试（每个用例使用独立临时 SQLite 文件）。

覆盖：PATCH 的等值 / 缺失条件、失败响应结构、事务回滚与 request_id 语义、
幂等指纹、与三方合并 / 契约校验的顺序、模型层 422，以及原子发布项的 guards。
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


def _create(client, content, doc_id="cfg"):
    r = client.post("/documents", json={"id": doc_id, "content": content})
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, doc_id, base_revision, request_id, patch, guards=None, author=None):
    body = {"base_revision": base_revision, "request_id": request_id, "patch": patch}
    if guards is not None:
        body["guards"] = guards
    if author is not None:
        body["author"] = author
    return client.patch(f"/documents/{doc_id}", json=body)


def _release(client, release_id, updates):
    return client.post(
        "/releases", json={"release_id": release_id, "updates": updates}
    )


def _item(doc_id, base_revision, patch, guards=None, author=None):
    body = {"document_id": doc_id, "base_revision": base_revision, "patch": patch}
    if guards is not None:
        body["guards"] = guards
    if author is not None:
        body["author"] = author
    return body


# ---------------- 等值条件（expected） ----------------


def test_equals_guard_passes_and_update_proceeds(client):
    _create(client, {"a": 1, "mode": "off"})
    r = _patch(client, "cfg", 1, "req-1", {"mode": "on"},
               guards=[{"path": "/mode", "expected": "off"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 2
    assert body["content"] == {"a": 1, "mode": "on"}


def test_equals_guard_failure_returns_409_with_expected_and_actual(client):
    _create(client, {"a": 1, "mode": "on"})
    r = _patch(client, "cfg", 1, "req-1", {"mode": "auto"},
               guards=[{"path": "/mode", "expected": "off"}])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "precondition_failed"
    assert detail["document_id"] == "cfg"
    assert detail["request_id"] == "req-1"
    assert detail["base_revision"] == 1
    assert detail["current_revision"] == 1
    assert detail["failures"] == [
        {"path": "/mode", "expected": "off", "actual": "on", "missing": []}
    ]


def test_equals_guard_failure_when_key_absent_marks_actual_missing(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-1", {"a": 2},
               guards=[{"path": "/gone", "expected": 5}])
    assert r.status_code == 409
    failure = r.json()["detail"]["failures"][0]
    assert failure == {"path": "/gone", "expected": 5, "actual": None,
                       "missing": ["actual"]}


def test_equals_guard_compares_deep_values(client):
    _create(client, {"srv": {"host": "h", "ports": [80, 443]}})
    # 键序不同、空白无关：深度相等即通过
    r = _patch(client, "cfg", 1, "req-1", {"x": 1},
               guards=[{"path": "/srv",
                        "expected": {"ports": [80, 443], "host": "h"}}])
    assert r.status_code == 200, r.text
    # 子树任一值不同即失败，actual 给出整个子树
    r = _patch(client, "cfg", 2, "req-2", {"x": 2},
               guards=[{"path": "/srv",
                        "expected": {"host": "h", "ports": [80]}}])
    assert r.status_code == 409
    failure = r.json()["detail"]["failures"][0]
    assert failure["actual"] == {"host": "h", "ports": [80, 443]}
    assert failure["missing"] == []


def test_equals_guard_strict_equality_distinguishes_true_and_one(client):
    _create(client, {"flag": True, "ratio": 1.0})
    # true 与 1 是不同值（规范化比较，不用 Python ==）
    r = _patch(client, "cfg", 1, "req-1", {"x": 1},
               guards=[{"path": "/flag", "expected": 1}])
    assert r.status_code == 409
    assert r.json()["detail"]["failures"][0]["actual"] is True
    # 1.0 与 1 同样视为不同
    r = _patch(client, "cfg", 1, "req-2", {"x": 1},
               guards=[{"path": "/ratio", "expected": 1}])
    assert r.status_code == 409
    # 精确相等的 1.0 通过
    r = _patch(client, "cfg", 1, "req-3", {"x": 1},
               guards=[{"path": "/ratio", "expected": 1.0}])
    assert r.status_code == 200, r.text


# ---------------- 缺失条件（missing: true） ----------------


def test_missing_guard_passes_when_key_absent(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-1", {"a": 2},
               guards=[{"path": "/legacy", "missing": True}])
    assert r.status_code == 200, r.text
    assert r.json()["content"] == {"a": 2}


def test_missing_guard_fails_when_key_present_marks_expected_missing(client):
    _create(client, {"a": 1, "legacy": {"x": 1}})
    r = _patch(client, "cfg", 1, "req-1", {"a": 2},
               guards=[{"path": "/legacy", "missing": True}])
    assert r.status_code == 409
    failure = r.json()["detail"]["failures"][0]
    assert failure == {"path": "/legacy", "expected": None,
                       "actual": {"x": 1}, "missing": ["expected"]}


def test_real_null_is_present_not_missing(client):
    _create(client, {"note": None})
    # 真实存储的 null 是存在的值：missing 条件不通过
    r = _patch(client, "cfg", 1, "req-1", {"a": 1},
               guards=[{"path": "/note", "missing": True}])
    assert r.status_code == 409
    failure = r.json()["detail"]["failures"][0]
    assert failure == {"path": "/note", "expected": None, "actual": None,
                       "missing": ["expected"]}
    # 但等值 null 条件可以通过（与键缺失区分）
    r = _patch(client, "cfg", 1, "req-2", {"a": 1},
               guards=[{"path": "/note", "expected": None}])
    assert r.status_code == 200, r.text


# ---------------- 指针形态 ----------------


def test_root_pointer_guard_evaluates_whole_document(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-1", {"a": 2},
               guards=[{"path": "", "expected": {"a": 1}}])
    assert r.status_code == 200, r.text
    r = _patch(client, "cfg", 2, "req-2", {"a": 3},
               guards=[{"path": "", "expected": {"a": 1}}])
    assert r.status_code == 409
    assert r.json()["detail"]["failures"][0]["path"] == ""


def test_nested_and_array_index_guards(client):
    _create(client, {"db": {"hosts": ["a", "b"]}, "t~x": {"a/b": 7}})
    # 数组下标与 ~0/~1 转义都按 RFC 6901 解释
    r = _patch(client, "cfg", 1, "req-1", {"z": 1}, guards=[
        {"path": "/db/hosts/1", "expected": "b"},
        {"path": "/t~0x/a~1b", "expected": 7},
        {"path": "/db/hosts/2", "missing": True},
    ])
    assert r.status_code == 200, r.text


def test_multiple_guard_failures_all_listed_in_input_order(client):
    _create(client, {"a": 1, "b": 2})
    r = _patch(client, "cfg", 1, "req-1", {"c": 3}, guards=[
        {"path": "/a", "expected": 100},   # 值不同
        {"path": "/b", "expected": 2},     # 通过
        {"path": "/ghost", "expected": 1},  # 实际缺失
        {"path": "/a", "missing": True},   # 期望缺失但存在
    ])
    assert r.status_code == 409
    failures = r.json()["detail"]["failures"]
    assert [(f["path"], f["missing"]) for f in failures] == [
        ("/a", []),
        ("/ghost", ["actual"]),
        ("/a", ["expected"]),
    ]


# ---------------- 求值时机与顺序 ----------------


def test_guards_evaluated_against_current_revision_not_base(client):
    _create(client, {"a": 1, "b": 2})
    # 服务端前进：b 改为 20（当前 rev2）
    assert _patch(client, "cfg", 1, "srv", {"b": 20}).status_code == 200
    # 来稿基于 rev1：guard 针对当前修订（b=20）求值，而非基线（b=2）
    r = _patch(client, "cfg", 1, "cli", {"a": 9},
               guards=[{"path": "/b", "expected": 20}])
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is True
    assert r.json()["content"] == {"a": 9, "b": 20}
    # 按基线值声明 guard 则不通过（当前已是 20）
    r = _patch(client, "cfg", 1, "cli-2", {"a": 8},
               guards=[{"path": "/b", "expected": 2}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "precondition_failed"


def test_guard_failure_precedes_unknown_base_and_conflict(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "srv", {"a": 10})
    # 基线不存在 + guard 失败：guards 先求值 → 409 precondition_failed
    r = _patch(client, "cfg", 99, "req-1", {"a": 1},
               guards=[{"path": "/a", "expected": 999}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "precondition_failed"
    # 三方分歧 + guard 失败：同样先报 guards
    r = _patch(client, "cfg", 1, "req-2", {"a": 20},
               guards=[{"path": "/a", "expected": 999}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "precondition_failed"
    # guard 通过后才暴露三方冲突
    r = _patch(client, "cfg", 1, "req-3", {"a": 20},
               guards=[{"path": "/a", "expected": 10}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "conflict"


def test_guard_failure_precedes_contract_validation(client):
    _create(client, {"a": 1})
    client.put("/documents/cfg/schema", json={"schema": {
        "type": "object", "properties": {"a": {"type": "integer", "minimum": 0}},
    }})
    # guard 失败 + patch 也会违反契约：先报 guards
    r = _patch(client, "cfg", 1, "req-1", {"a": -5},
               guards=[{"path": "/a", "expected": 999}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "precondition_failed"
    # guard 通过、候选违反契约：仍是 422 schema_validation_failed
    r = _patch(client, "cfg", 1, "req-2", {"a": -5},
               guards=[{"path": "/a", "expected": 1}])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "schema_validation_failed"


# ---------------- 回滚与 request_id ----------------


def test_guard_failure_writes_nothing_and_does_not_consume_request_id(client):
    _create(client, {"a": 1})
    before = client.get("/documents/cfg").json()
    r = _patch(client, "cfg", 1, "req-x", {"a": 2},
               guards=[{"path": "/a", "expected": 999}])
    assert r.status_code == 409
    # 内容与修订号不变
    assert client.get("/documents/cfg").json() == before
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1]
    # request_id 未被占用：修正条件后可用同一 ID 重试
    r = _patch(client, "cfg", 1, "req-x", {"a": 2},
               guards=[{"path": "/a", "expected": 1}])
    assert r.status_code == 200, r.text
    assert r.json()["content"] == {"a": 2}
    # 也可不带 guards 用同一 ID（首次失败未落指纹）
    _create(client, {"b": 1}, doc_id="cfg2")
    assert _patch(client, "cfg2", 1, "req-y", {"b": 2},
                  guards=[{"path": "/b", "expected": 0}]).status_code == 409
    assert _patch(client, "cfg2", 1, "req-y", {"b": 2}).status_code == 200


# ---------------- 幂等指纹 ----------------


def test_guards_are_part_of_idempotency_fingerprint(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "dup", {"a": 7},
                  guards=[{"path": "/a", "expected": 1}]).status_code == 200
    # 同 request_id + 同 (base, patch) 但 guards 不同 → 409
    r = _patch(client, "cfg", 1, "dup", {"a": 7},
               guards=[{"path": "/a", "expected": 7}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"
    # 去掉 guards 也算不同请求
    r = _patch(client, "cfg", 1, "dup", {"a": 7})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"
    # 完全相同（含 guards）→ 幂等重放
    r = _patch(client, "cfg", 1, "dup", {"a": 7},
               guards=[{"path": "/a", "expected": 1}])
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["revision"] == 2


def test_guard_order_and_condition_kind_matter_for_fingerprint(client):
    _create(client, {"a": 1, "b": 2})
    guards = [{"path": "/a", "expected": 1}, {"path": "/b", "missing": False,
                                              "expected": 2}]
    assert _patch(client, "cfg", 1, "g1", {"x": 1}, guards=guards).status_code == 200
    # missing:false 显式声明与省略等价 → 幂等重放
    r = _patch(client, "cfg", 1, "g1", {"x": 1},
               guards=[{"path": "/a", "expected": 1},
                       {"path": "/b", "expected": 2}])
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    # 换序视为不同请求
    r = _patch(client, "cfg", 1, "g1", {"x": 1},
               guards=[{"path": "/b", "expected": 2},
                       {"path": "/a", "expected": 1}])
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "request_id_conflict"


def test_empty_guards_array_is_equivalent_to_omitted(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "e1", {"a": 2}).status_code == 200
    # 空数组与省略 guards 指纹相同 → 幂等重放而非冲突
    r = _patch(client, "cfg", 1, "e1", {"a": 2}, guards=[])
    assert r.status_code == 200
    assert r.json()["idempotent"] is True


def test_idempotent_replay_does_not_reevaluate_guards(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "idem", {"a": 2},
                  guards=[{"path": "/a", "expected": 1}]).status_code == 200
    # 文档已前进，guard 此刻不再成立；重放仍返回首次结果
    r = _patch(client, "cfg", 1, "idem", {"a": 2},
               guards=[{"path": "/a", "expected": 1}])
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    assert r.json()["content"] == {"a": 2}
    hist = client.get("/documents/cfg/revisions").json()["items"]
    assert [h["revision"] for h in hist] == [1, 2]


# ---------------- 模型层 422 ----------------


def test_invalid_pointer_returns_422(client):
    _create(client, {"a": 1})
    for bad in ("a", "//ok-but-not-this~2", "/a~", "/a~x"):
        r = _patch(client, "cfg", 1, "req-bad", {"a": 2},
                   guards=[{"path": bad, "expected": 1}])
        assert r.status_code == 422, bad
    # 文档未被修改
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_declaring_both_conditions_returns_422(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-both", {"a": 2},
               guards=[{"path": "/a", "expected": 1, "missing": True}])
    assert r.status_code == 422
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_declaring_no_condition_returns_422(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-none", {"a": 2},
               guards=[{"path": "/a"}])
    assert r.status_code == 422
    # missing:false 不算声明缺失条件
    r = _patch(client, "cfg", 1, "req-none2", {"a": 2},
               guards=[{"path": "/a", "missing": False}])
    assert r.status_code == 422


def test_guard_with_unknown_field_returns_422(client):
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-extra", {"a": 2},
               guards=[{"path": "/a", "expected": 1, "bogus": 1}])
    assert r.status_code == 422


# ---------------- 原子发布 ----------------


def test_release_item_guards_pass(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 2}, "d2")
    r = _release(client, "rel-g", [
        _item("d1", 1, {"a": 10}, guards=[{"path": "/a", "expected": 1}]),
        _item("d2", 1, {"b": 20}, guards=[{"path": "/legacy", "missing": True}]),
    ])
    assert r.status_code == 200, r.text
    results = {x["document_id"]: x for x in r.json()["results"]}
    assert results["d1"]["content"] == {"a": 10}
    assert results["d2"]["content"] == {"b": 20}


def test_release_item_guard_failure_aggregates_and_rolls_back(client):
    _create(client, {"a": 1}, "ok")
    _create(client, {"b": 2}, "bad")
    before_ok = client.get("/documents/ok").json()
    r = _release(client, "rel-fail", [
        _item("ok", 1, {"a": 100}),
        _item("bad", 1, {"b": 200},
              guards=[{"path": "/b", "expected": 999},
                      {"path": "/b", "missing": True}]),
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "release_failed"
    assert detail["release_id"] == "rel-fail"
    assert len(detail["failures"]) == 1
    failure = detail["failures"][0]
    assert failure["index"] == 1
    assert failure["document_id"] == "bad"
    assert failure["error"] == "precondition_failed"
    assert failure["status_code"] == 409
    assert [(f["path"], f["missing"]) for f in failure["failures"]] == [
        ("/b", []),
        ("/b", ["expected"]),
    ]
    # 整批回滚：本可成功的第一项也不产生修订
    assert client.get("/documents/ok").json() == before_ok
    assert client.get("/documents/bad").json()["revision"] == 1
    # release_id 未被占用：修正后同 ID 重试成功
    r = _release(client, "rel-fail", [
        _item("ok", 1, {"a": 100}),
        _item("bad", 1, {"b": 200}, guards=[{"path": "/b", "expected": 2}]),
    ])
    assert r.status_code == 200, r.text


def test_release_guards_are_part_of_batch_fingerprint(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 2}, "d2")
    req = [
        _item("d1", 1, {"a": 7}, guards=[{"path": "/a", "expected": 1}]),
        _item("d2", 1, {"b": 8}),
    ]
    assert _release(client, "rel-idem", req).status_code == 200
    # 相同请求（含 guards）→ 幂等重放
    r = _release(client, "rel-idem", req)
    assert r.status_code == 200
    assert r.json()["idempotent"] is True
    # 任一项 guards 变化 → 409 release_id_conflict
    changed = [
        _item("d1", 1, {"a": 7}, guards=[{"path": "/a", "expected": 7}]),
        _item("d2", 1, {"b": 8}),
    ]
    r = _release(client, "rel-idem", changed)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "release_id_conflict"
    # 去掉 guards 也算不同请求
    r = _release(client, "rel-idem",
                 [_item("d1", 1, {"a": 7}), _item("d2", 1, {"b": 8})])
    assert r.status_code == 409
    # 冲突重放不得再写任何修订
    assert client.get("/documents/d1").json()["revision"] == 2


def test_release_invalid_guard_shape_returns_422_and_writes_nothing(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 2}, "d2")
    r = _release(client, "rel-bad", [
        _item("d1", 1, {"a": 7}),
        _item("d2", 1, {"b": 8},
              guards=[{"path": "/b", "expected": 2, "missing": True}]),
    ])
    assert r.status_code == 422
    assert client.get("/documents/d1").json()["revision"] == 1
    # 模型层拒绝，release_id 未占用：修正后可同 ID 成功
    r = _release(client, "rel-bad", [
        _item("d1", 1, {"a": 7}),
        _item("d2", 1, {"b": 8}, guards=[{"path": "/b", "expected": 2}]),
    ])
    assert r.status_code == 200, r.text
