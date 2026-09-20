"""POST /bundles/lock 跨文档依赖锁定包集成测试（独立临时数据库）。

锁定为只读分析：在同一读事务快照内固定根文档与所有 @current 引用的修订，
递归收集依赖闭包（按 文档+修订 去重），返回排序稳定的根列表、原始快照与
依赖边；成功或失败都不产生任何写入。
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


def _lock(client, roots):
    return client.post("/bundles/lock", json={"roots": roots})


def _revision_count(client, doc_id):
    r = client.get(f"/documents/{doc_id}/revisions", params={"limit": 200})
    assert r.status_code == 200, r.text
    return len(r.json()["items"])


# ---------------- 基本锁定：固定、快照、依赖边 ----------------


def test_lock_root_without_refs(client):
    _create(client, {"a": 1, "n": {"l": [True, None]}})
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roots"] == [{"document_id": "cfg", "revision": 1}]
    assert body["snapshots"] == [
        {
            "document_id": "cfg",
            "revision": 1,
            "content": {"a": 1, "n": {"l": [True, None]}},
        }
    ]
    assert body["edges"] == []
    assert len(body["bundle_digest"]) == 64
    int(body["bundle_digest"], 16)  # 合法十六进制


def test_lock_pins_current_refs_to_numeric_revisions(client):
    _create(client, {"host": "a"}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"host": "b"})  # dep → rev2
    _create(client, {"db": {"$ref": "doc:dep@current#"}, "debug": False}, doc_id="app")

    r = _lock(client, [{"document_id": "app"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["roots"] == [{"document_id": "app", "revision": 1}]
    # 快照为原始内容：引用保持 $ref 形态，不展开
    assert body["snapshots"] == [
        {
            "document_id": "app",
            "revision": 1,
            "content": {"db": {"$ref": "doc:dep@current#"}, "debug": False},
        },
        {"document_id": "dep", "revision": 2, "content": {"host": "b"}},
    ]
    # @current 固定为当次快照所见的数字修订；ref 保留原始引用串
    assert body["edges"] == [
        {
            "from": {"document_id": "app", "revision": 1, "at": "/db"},
            "to": {"document_id": "dep", "revision": 2, "pointer": ""},
            "ref": "doc:dep@current#",
        }
    ]


def test_numeric_refs_stay_unchanged(client):
    _create(client, {"v": 1}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"v": 2})
    _create(client, {"v": {"$ref": "doc:dep@1#/v"}}, doc_id="app")

    body = _lock(client, [{"document_id": "app"}]).json()
    assert body["edges"] == [
        {
            "from": {"document_id": "app", "revision": 1, "at": "/v"},
            "to": {"document_id": "dep", "revision": 1, "pointer": "/v"},
            "ref": "doc:dep@1#/v",
        }
    ]
    assert body["snapshots"] == [
        {"document_id": "app", "revision": 1, "content": {"v": {"$ref": "doc:dep@1#/v"}}},
        {"document_id": "dep", "revision": 1, "content": {"v": 1}},
    ]


def test_root_revision_defaults_to_current(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "p1", {"a": 2})
    body = _lock(client, [{"document_id": "cfg"}]).json()
    assert body["roots"] == [{"document_id": "cfg", "revision": 2}]
    assert body["snapshots"][0]["content"] == {"a": 2}


def test_historical_root_revision_locks_historical_closure(client):
    _create(client, {"x": "dep-value"}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/x"}, "tag": "old"}, doc_id="app")
    _patch(client, "app", 1, "p1", {"tag": "new", "v": None})  # rev2 移除引用

    body = _lock(client, [{"document_id": "app", "revision": 1}]).json()
    assert body["roots"] == [{"document_id": "app", "revision": 1}]
    assert [s["document_id"] for s in body["snapshots"]] == ["app", "dep"]
    assert body["edges"][0]["to"] == {
        "document_id": "dep",
        "revision": 1,
        "pointer": "/x",
    }
    # 默认 current（rev2）的闭包不再包含 dep，摘要不同
    current = _lock(client, [{"document_id": "app"}]).json()
    assert current["roots"] == [{"document_id": "app", "revision": 2}]
    assert [s["document_id"] for s in current["snapshots"]] == ["app"]
    assert current["bundle_digest"] != body["bundle_digest"]


def test_transitive_dependencies_collected(client):
    _create(client, {"v": 7}, doc_id="leaf")
    _create(client, {"inner": {"$ref": "doc:leaf@1#/v"}}, doc_id="mid")
    _create(client, {"outer": {"$ref": "doc:mid@1#/inner"}}, doc_id="app")

    body = _lock(client, [{"document_id": "app"}]).json()
    assert [s["document_id"] for s in body["snapshots"]] == ["app", "leaf", "mid"]
    assert body["edges"] == [
        {
            "from": {"document_id": "app", "revision": 1, "at": "/outer"},
            "to": {"document_id": "mid", "revision": 1, "pointer": "/inner"},
            "ref": "doc:mid@1#/inner",
        },
        {
            "from": {"document_id": "mid", "revision": 1, "at": "/inner"},
            "to": {"document_id": "leaf", "revision": 1, "pointer": "/v"},
            "ref": "doc:leaf@1#/v",
        },
    ]


def test_diamond_snapshots_deduped(client):
    _create(client, {"leaf": "L"}, doc_id="d")
    _create(client, {"from_b": {"$ref": "doc:d@1#/leaf"}}, doc_id="b")
    _create(client, {"from_c": {"$ref": "doc:d@1#/leaf"}}, doc_id="c")
    _create(
        client,
        {"l": {"$ref": "doc:b@1#/from_b"}, "r": {"$ref": "doc:c@1#/from_c"}},
        doc_id="app",
    )

    body = _lock(client, [{"document_id": "app"}]).json()
    # 共享依赖 d 只出现一次；快照按 (文档, 修订) 排序
    assert [s["document_id"] for s in body["snapshots"]] == ["app", "b", "c", "d"]
    assert len(body["edges"]) == 4


def test_whole_snapshot_traversed_beyond_pointed_value(client):
    # 锁定包遍历整篇快照：dep 内 /a 之外的引用同样进入闭包
    _create(client, {"other": True}, doc_id="other")
    _create(client, {"a": 1, "b": {"$ref": "doc:other@1#"}}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a"}}, doc_id="app")

    body = _lock(client, [{"document_id": "app"}]).json()
    assert [s["document_id"] for s in body["snapshots"]] == ["app", "dep", "other"]
    assert body["edges"] == [
        {
            "from": {"document_id": "app", "revision": 1, "at": "/v"},
            "to": {"document_id": "dep", "revision": 1, "pointer": "/a"},
            "ref": "doc:dep@1#/a",
        },
        {
            "from": {"document_id": "dep", "revision": 1, "at": "/b"},
            "to": {"document_id": "other", "revision": 1, "pointer": ""},
            "ref": "doc:other@1#",
        },
    ]


def test_refs_inside_arrays(client):
    _create(client, ["zero", "one"], doc_id="arr")
    _create(client, {"list": [1, {"$ref": "doc:arr@1#/1"}, 3]}, doc_id="app")

    body = _lock(client, [{"document_id": "app"}]).json()
    assert body["snapshots"] == [
        {"document_id": "app", "revision": 1,
         "content": {"list": [1, {"$ref": "doc:arr@1#/1"}, 3]}},
        {"document_id": "arr", "revision": 1, "content": ["zero", "one"]},
    ]
    assert body["edges"] == [
        {
            "from": {"document_id": "app", "revision": 1, "at": "/list/1"},
            "to": {"document_id": "arr", "revision": 1, "pointer": "/1"},
            "ref": "doc:arr@1#/1",
        }
    ]


def test_escaped_pointer_segments(client):
    _create(client, {"a/b": {"t~k": "hit"}}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a~1b/t~0k"}}, doc_id="app")

    r = _lock(client, [{"document_id": "app"}])
    assert r.status_code == 200, r.text
    assert r.json()["edges"][0]["to"]["pointer"] == "/a~1b/t~0k"


def test_non_reference_ref_objects_ignored(client):
    content = {
        "extra": {"$ref": "doc:ghost@1#/a", "other": 1},  # 含额外键 → 不是引用
        "bad1": {"$ref": "doc:no-at-sign"},
        "bad2": {"$ref": "doc:x@0#/a"},
        "bad3": {"$ref": "https://example.com/x"},
        "bad4": {"$ref": 5},
        "bad5": {"$ref": "doc:x@1"},
    }
    _create(client, content)
    body = _lock(client, [{"document_id": "cfg"}]).json()
    assert body["edges"] == []
    assert body["snapshots"] == [
        {"document_id": "cfg", "revision": 1, "content": content}
    ]


def test_cycle_collected_as_edges_not_error(client):
    # 锁定包是依赖图快照：成环只是普通边，不像解析那样报 409
    _create(client, {"to_b": {"$ref": "doc:b@1#/x"}}, doc_id="a")
    _create(client, {"x": {"to_a": {"$ref": "doc:a@1#/to_b"}}}, doc_id="b")

    r = _lock(client, [{"document_id": "a"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["document_id"] for s in body["snapshots"]] == ["a", "b"]
    assert body["edges"] == [
        {
            "from": {"document_id": "a", "revision": 1, "at": "/to_b"},
            "to": {"document_id": "b", "revision": 1, "pointer": "/x"},
            "ref": "doc:b@1#/x",
        },
        {
            "from": {"document_id": "b", "revision": 1, "at": "/x/to_a"},
            "to": {"document_id": "a", "revision": 1, "pointer": "/to_b"},
            "ref": "doc:a@1#/to_b",
        },
    ]


def test_self_cycle_single_edge(client):
    _create(client, {"x": {"$ref": "doc:cfg@current#/x"}})
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["snapshots"] == [
        {
            "document_id": "cfg",
            "revision": 1,
            "content": {"x": {"$ref": "doc:cfg@current#/x"}},
        }
    ]
    assert body["edges"] == [
        {
            "from": {"document_id": "cfg", "revision": 1, "at": "/x"},
            "to": {"document_id": "cfg", "revision": 1, "pointer": "/x"},
            "ref": "doc:cfg@current#/x",
        }
    ]


# ---------------- 稳定排序与摘要 ----------------


def test_root_order_does_not_change_response_or_digest(client):
    _create(client, {"shared": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@current#"}}, doc_id="app")
    _create(client, {"w": {"$ref": "doc:dep@current#"}}, doc_id="other")

    roots = [{"document_id": "app"}, {"document_id": "other"}]
    forward = _lock(client, roots).json()
    backward = _lock(client, list(reversed(roots))).json()
    assert forward == backward  # 整个响应体（含摘要）完全一致
    assert forward["roots"] == [
        {"document_id": "app", "revision": 1},
        {"document_id": "other", "revision": 1},
    ]


def test_duplicate_roots_deduped_after_pinning(client):
    _create(client, {"a": 1})
    single = _lock(client, [{"document_id": "cfg"}]).json()
    duplicated = _lock(
        client,
        [
            {"document_id": "cfg", "revision": "current"},
            {"document_id": "cfg", "revision": 1},
            {"document_id": "cfg"},
        ],
    ).json()
    assert duplicated["roots"] == [{"document_id": "cfg", "revision": 1}]
    assert duplicated == single


def test_current_and_explicit_revision_give_same_digest(client):
    _create(client, {"v": 1}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"v": 2})
    _create(client, {"v": {"$ref": "doc:dep@current#"}}, doc_id="app")

    by_current = _lock(client, [{"document_id": "app", "revision": "current"}]).json()
    by_number = _lock(client, [{"document_id": "app", "revision": 1}]).json()
    assert by_current == by_number
    assert by_current["bundle_digest"] == by_number["bundle_digest"]


def test_edges_sorted_by_document_revision_pointer(client):
    _create(client, {"leaf": 1}, doc_id="leaf")
    _create(client, {"ref": {"$ref": "doc:leaf@1#"}}, doc_id="a")
    _create(client, {"ref": {"$ref": "doc:leaf@1#"}}, doc_id="m")
    # 字典序先 m 后 a，验证输出按排序而非遍历顺序
    _create(
        client,
        {"m": {"$ref": "doc:m@1#"}, "a": {"$ref": "doc:a@1#"}},
        doc_id="root",
    )

    body = _lock(client, [{"document_id": "root"}]).json()
    assert [(e["from"]["document_id"], e["from"]["at"]) for e in body["edges"]] == [
        ("a", "/ref"),
        ("m", "/ref"),
        ("root", "/a"),
        ("root", "/m"),
    ]


def test_digest_changes_with_content(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@current#"}}, doc_id="app")
    before = _lock(client, [{"document_id": "app"}]).json()

    _patch(client, "dep", 1, "p1", {"v": 2})
    after = _lock(client, [{"document_id": "app"}]).json()
    # @current 固定到更新的修订，闭包内容变化 → 摘要变化
    assert after["edges"][0]["to"]["revision"] == 2
    assert after["bundle_digest"] != before["bundle_digest"]


# ---------------- 只读保证 ----------------


def test_lock_produces_no_writes(client):
    _create(client, {"v": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@current#"}}, doc_id="app")

    r = _lock(client, [{"document_id": "app"}])
    assert r.status_code == 200, r.text

    assert _revision_count(client, "dep") == 1
    assert _revision_count(client, "app") == 1
    assert client.get("/documents/dep").json()["content"] == {"v": 1}
    assert client.get("/documents/app").json()["content"] == {
        "v": {"$ref": "doc:dep@current#"}
    }


def test_failed_lock_produces_no_writes(client):
    _create(client, {"v": {"$ref": "doc:ghost@current#"}}, doc_id="app")

    r = _lock(client, [{"document_id": "app"}])
    assert r.status_code == 422

    assert _revision_count(client, "app") == 1
    # 失败后任意 request_id 未被占用：正常 PATCH 成功且非幂等重放
    p = _patch(client, "app", 1, "req-after-failed-lock", {"v": 1})
    assert p["idempotent"] is False
    assert p["revision"] == 2


# ---------------- 422：文档 / 修订 / 指针不存在 ----------------


def test_missing_root_document_returns_422_with_chain(client):
    r = _lock(client, [{"document_id": "ghost"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {"document_id": "ghost", "revision": None, "pointer": "", "ref": None, "at": None}
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
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {"document_id": "cfg", "revision": 1, "pointer": "", "ref": None, "at": None},
        {
            "document_id": "ghost",
            "revision": None,
            "pointer": "/a",
            "ref": "doc:ghost@current#/a",
            "at": "/v",
        },
    ]


def test_missing_referenced_revision_returns_422(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@9#/a"}})
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["document_id"] == "dep"
    assert detail["revision"] == 9
    assert detail["current_revision"] == 1
    assert detail["chain"][-1] == {
        "document_id": "dep",
        "revision": 9,
        "pointer": "/a",
        "ref": "doc:dep@9#/a",
        "at": "/v",
    }


def test_missing_pointer_returns_422_with_full_chain(client):
    _create(client, {"x": 1}, doc_id="c")
    _create(client, {"via": {"$ref": "doc:c@1#/missing"}}, doc_id="b")
    _create(client, {"top": {"$ref": "doc:b@1#/via"}})
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "pointer_not_found"
    # 完整引用链：根 → b → c（失败一跳）
    assert [(h["document_id"], h["pointer"]) for h in detail["chain"]] == [
        ("cfg", ""),
        ("b", "/via"),
        ("c", "/missing"),
    ]
    assert detail["chain"][1]["at"] == "/top"
    assert detail["chain"][2]["at"] == "/via"


def test_invalid_pointer_escape_returns_422(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a~2b"}})
    r = _lock(client, [{"document_id": "cfg"}])
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pointer_not_found"


def test_second_root_failure_reports_its_own_chain(client):
    _create(client, {"ok": 1}, doc_id="good")
    r = _lock(client, [{"document_id": "good"}, {"document_id": "ghost"}])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["chain"] == [
        {"document_id": "ghost", "revision": None, "pointer": "", "ref": None, "at": None}
    ]


# ---------------- 请求模型校验 ----------------


def test_roots_count_validated(client):
    assert _lock(client, []).status_code == 422
    too_many = [{"document_id": f"d{i}"} for i in range(21)]
    assert _lock(client, too_many).status_code == 422
    # 上限 20 个在模型层通过（文档不存在 → document_not_found）
    exactly_max = [{"document_id": f"d{i}"} for i in range(20)]
    r = _lock(client, exactly_max)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "document_not_found"


def test_revision_values_validated(client):
    _create(client, {"a": 1})
    for bad in (0, -1, "0", "01", "1.5", "abc", "", "CURRENT", 1.5, True, None):
        r = _lock(client, [{"document_id": "cfg", "revision": bad}])
        assert r.status_code == 422, bad
    # 合法形态：current（含省略）与正整数
    assert _lock(client, [{"document_id": "cfg", "revision": "current"}]).status_code == 200
    assert _lock(client, [{"document_id": "cfg", "revision": 1}]).status_code == 200
    assert _lock(client, [{"document_id": "cfg"}]).status_code == 200


def test_extra_fields_rejected(client):
    _create(client, {"a": 1})
    r = client.post(
        "/bundles/lock",
        json={"roots": [{"document_id": "cfg"}], "bogus": 1},
    )
    assert r.status_code == 422
    r = client.post(
        "/bundles/lock",
        json={"roots": [{"document_id": "cfg", "extra": 1}]},
    )
    assert r.status_code == 422
    r = client.post("/bundles/lock", json={"roots": [{"revision": 1}]})
    assert r.status_code == 422


def test_document_id_pattern_validated(client):
    for bad in ("", "-bad", ".bad", "has space", "slash/x"):
        r = _lock(client, [{"document_id": bad}])
        assert r.status_code == 422, bad
