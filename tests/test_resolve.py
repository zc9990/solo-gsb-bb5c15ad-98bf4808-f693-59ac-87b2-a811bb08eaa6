"""GET /documents/{id}/resolved 跨文档引用解析集成测试（独立临时数据库）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.resolve import MAX_DEPTH


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


def _resolved(client, doc_id="cfg", **params):
    return client.get(f"/documents/{doc_id}/resolved", params=params)


# ---------------- 基本解析 ----------------


def test_simple_ref_resolved_and_dependencies_listed(client):
    _create(client, {"a": {"x": 1}, "b": 2}, doc_id="dep")
    _create(client, {"cfg": {"$ref": "doc:dep@1#/a"}, "own": True})
    r = _resolved(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "cfg"
    assert body["revision"] == 1
    assert body["resolved"] == {"cfg": {"x": 1}, "own": True}
    assert body["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": "/a"}
    ]


def test_ref_inside_array_element(client):
    _create(client, ["zero", "one"], doc_id="arr")
    _create(client, {"list": [1, {"$ref": "doc:arr@1#/1"}, 3]})
    body = _resolved(client).json()
    assert body["resolved"] == {"list": [1, "one", 3]}
    assert body["dependencies"] == [
        {"document_id": "arr", "revision": 1, "pointer": "/1"}
    ]


def test_whole_document_ref_with_empty_pointer(client):
    _create(client, {"x": 1, "y": [2]}, doc_id="dep")
    _create(client, {"$ref": "doc:dep@1#"})
    body = _resolved(client).json()
    assert body["resolved"] == {"x": 1, "y": [2]}
    assert body["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": ""}
    ]


def test_nested_refs_resolve_recursively(client):
    _create(client, {"v": 7}, doc_id="c")
    _create(client, {"inner": {"$ref": "doc:c@1#/v"}}, doc_id="b")
    _create(client, {"outer": {"$ref": "doc:b@1#/inner"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"outer": 7}
    # 按首次遍历排序：先 b 后 c
    assert body["dependencies"] == [
        {"document_id": "b", "revision": 1, "pointer": "/inner"},
        {"document_id": "c", "revision": 1, "pointer": "/v"},
    ]


def test_ref_whose_target_is_itself_a_ref(client):
    _create(client, 42, doc_id="leaf")
    _create(client, {"alias": {"$ref": "doc:leaf@1#"}}, doc_id="mid")
    _create(client, {"v": {"$ref": "doc:mid@1#/alias"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"v": 42}


def test_escaped_pointer_segments(client):
    _create(client, {"a/b": {"t~k": "hit"}}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a~1b/t~0k"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"v": "hit"}


def test_non_reference_ref_objects_are_left_untouched(client):
    content = {
        # 含额外键 → 不是引用
        "extra": {"$ref": "doc:ghost@1#/a", "other": 1},
        # 值不匹配引用形态 → 普通数据
        "bad1": {"$ref": "doc:no-at-sign"},
        "bad2": {"$ref": "doc:x@0#/a"},
        "bad3": {"$ref": "https://example.com/x"},
        "bad4": {"$ref": 5},
        "bad5": {"$ref": "doc:x@1"},
    }
    _create(client, content)
    body = _resolved(client).json()
    assert body["resolved"] == content
    assert body["dependencies"] == []


def test_document_without_refs_resolves_to_itself(client):
    _create(client, {"a": 1, "n": {"l": [True, None]}})
    body = _resolved(client).json()
    assert body["resolved"] == {"a": 1, "n": {"l": [True, None]}}
    assert body["dependencies"] == []
    assert body["revision"] == 1


# ---------------- 修订固定（current / 指定修订） ----------------


def test_current_ref_pins_latest_revision_in_snapshot(client):
    _create(client, {"v": 1}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"v": 2})
    _create(client, {"v": {"$ref": "doc:dep@current#"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"v": {"v": 2}}
    assert body["dependencies"] == [
        {"document_id": "dep", "revision": 2, "pointer": ""}
    ]
    # 依赖文档继续前进后，再次解析跟随新的 current
    _patch(client, "dep", 2, "p2", {"v": 3})
    body = _resolved(client).json()
    assert body["resolved"] == {"v": {"v": 3}}
    assert body["dependencies"][0]["revision"] == 3


def test_explicit_revision_ref_uses_historical_content(client):
    _create(client, {"v": 1}, doc_id="dep")
    _patch(client, "dep", 1, "p1", {"v": 2})
    _create(client, {"v": {"$ref": "doc:dep@1#/v"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"v": 1}
    assert body["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": "/v"}
    ]


def test_root_revision_param_selects_historical_root(client):
    _create(client, {"v": {"$ref": "doc:dep@1#/x"}, "tag": "old"})
    _create(client, {"x": "dep-value"}, doc_id="dep")
    _patch(client, "cfg", 1, "p1", {"tag": "new"})
    # 默认 current：根取 rev2
    body = _resolved(client).json()
    assert body["revision"] == 2
    assert body["resolved"]["tag"] == "new"
    # 指定根修订：解析 rev1 的内容
    body = _resolved(client, revision="1").json()
    assert body["revision"] == 1
    assert body["resolved"] == {"v": "dep-value", "tag": "old"}


def test_root_revision_param_current_is_default(client):
    _create(client, {"a": 1})
    _patch(client, "cfg", 1, "p1", {"a": 2})
    assert _resolved(client).json()["resolved"] == {"a": 2}
    assert _resolved(client, revision="current").json()["resolved"] == {"a": 2}


def test_ref_to_older_revision_of_same_document_is_not_a_cycle(client):
    _create(client, {"x": 1})
    _patch(client, "cfg", 1, "p1", {"self": {"$ref": "doc:cfg@1#/x"}})
    body = _resolved(client).json()
    assert body["resolved"] == {"x": 1, "self": 1}
    assert body["dependencies"] == [
        {"document_id": "cfg", "revision": 1, "pointer": "/x"}
    ]


# ---------------- 依赖清单：去重与首次遍历顺序 ----------------


def test_diamond_dependencies_deduped_in_first_traversal_order(client):
    _create(client, {"leaf": "L"}, doc_id="d")
    _create(client, {"from_b": {"$ref": "doc:d@1#/leaf"}}, doc_id="b")
    _create(client, {"from_c": {"$ref": "doc:d@1#/leaf"}}, doc_id="c")
    _create(
        client,
        {
            "first": {"$ref": "doc:b@1#/from_b"},
            "second": {"$ref": "doc:c@1#/from_c"},
        },
    )
    body = _resolved(client).json()
    assert body["resolved"] == {"first": "L", "second": "L"}
    # 前序遍历首次出现顺序：b → d → c（d 去重）
    assert body["dependencies"] == [
        {"document_id": "b", "revision": 1, "pointer": "/from_b"},
        {"document_id": "d", "revision": 1, "pointer": "/leaf"},
        {"document_id": "c", "revision": 1, "pointer": "/from_c"},
    ]


def test_same_target_referenced_twice_resolves_consistently(client):
    _create(client, {"shared": [1, 2]}, doc_id="dep")
    _create(
        client,
        {
            "a": {"$ref": "doc:dep@1#/shared"},
            "b": {"$ref": "doc:dep@1#/shared"},
        },
    )
    body = _resolved(client).json()
    assert body["resolved"] == {"a": [1, 2], "b": [1, 2]}
    assert body["dependencies"] == [
        {"document_id": "dep", "revision": 1, "pointer": "/shared"}
    ]


def test_resolution_does_not_modify_stored_documents(client):
    _create(client, {"x": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/x"}})
    _resolved(client)
    # 原文档内容保持引用形态，解析只是只读视图
    assert client.get("/documents/cfg").json()["content"] == {
        "v": {"$ref": "doc:dep@1#/x"}
    }
    assert client.get("/documents/cfg").json()["revision"] == 1


# ---------------- 422：文档 / 修订 / 指针不存在 ----------------


def test_missing_root_document_returns_422_with_chain(client):
    r = _resolved(client, doc_id="ghost")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {
            "document_id": "ghost",
            "revision": None,
            "pointer": "",
            "ref": None,
            "at": None,
        }
    ]


def test_missing_referenced_document_returns_422_with_full_chain(client):
    _create(client, {"v": {"$ref": "doc:ghost@current#/a"}})
    r = _resolved(client)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "document_not_found"
    assert detail["document_id"] == "ghost"
    assert detail["chain"] == [
        {
            "document_id": "cfg",
            "revision": 1,
            "pointer": "",
            "ref": None,
            "at": None,
        },
        {
            "document_id": "ghost",
            "revision": None,
            "pointer": "/a",
            "ref": "doc:ghost@current#/a",
            "at": "/v",
        },
    ]


def test_missing_revision_returns_422_with_chain(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@9#/a"}})
    r = _resolved(client)
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


def test_missing_root_revision_returns_422(client):
    _create(client, {"a": 1})
    r = _resolved(client, revision="9")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "revision_not_found"
    assert detail["revision"] == 9
    assert detail["current_revision"] == 1
    assert detail["chain"] == [
        {
            "document_id": "cfg",
            "revision": 9,
            "pointer": "",
            "ref": None,
            "at": None,
        }
    ]


def test_missing_pointer_returns_422_with_full_chain(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/nope"}})
    r = _resolved(client)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "pointer_not_found"
    assert detail["document_id"] == "dep"
    assert detail["revision"] == 1
    assert detail["pointer"] == "/nope"
    assert [hop["document_id"] for hop in detail["chain"]] == ["cfg", "dep"]


def test_pointer_failure_chain_spans_multiple_hops(client):
    _create(client, {"x": 1}, doc_id="c")
    _create(client, {"via": {"$ref": "doc:c@1#/missing"}}, doc_id="b")
    _create(client, {"top": {"$ref": "doc:b@1#/via"}})
    r = _resolved(client)
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
    assert detail["chain"][2]["at"] == ""


def test_pointer_array_index_out_of_range_returns_422(client):
    _create(client, ["only"], doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/3"}})
    r = _resolved(client)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pointer_not_found"


def test_pointer_into_scalar_returns_422(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a/b"}})
    r = _resolved(client)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pointer_not_found"


def test_pointer_with_invalid_escape_returns_422(client):
    _create(client, {"a": 1}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#/a~2b"}})
    r = _resolved(client)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pointer_not_found"


def test_invalid_revision_param_returns_422(client):
    _create(client, {"a": 1})
    for bad in ("0", "-1", "01", "1.5", "abc", "", "CURRENT"):
        r = _resolved(client, revision=bad)
        assert r.status_code == 422, bad
        assert r.json()["detail"]["error"] == "invalid_revision"


# ---------------- 409：依赖成环 ----------------


def test_direct_self_cycle_returns_409_with_closed_chain(client):
    _create(client, {"x": {"$ref": "doc:cfg@current#/x"}})
    r = _resolved(client)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "reference_cycle"
    chain = detail["chain"]
    # 首尾闭合：链首与链尾是同一 (文档, 修订, 指针)
    assert len(chain) == 2
    assert chain[0]["document_id"] == chain[-1]["document_id"] == "cfg"
    assert chain[0]["revision"] == chain[-1]["revision"] == 1
    assert chain[0]["pointer"] == chain[-1]["pointer"] == "/x"


def test_two_document_cycle_returns_409(client):
    _create(client, {"to_b": {"$ref": "doc:b@1#/x"}}, doc_id="a")
    _create(client, {"x": {"to_a": {"$ref": "doc:a@1#/to_b"}}}, doc_id="b")
    r = _resolved(client, doc_id="a")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "reference_cycle"
    chain = detail["chain"]
    assert chain[0]["document_id"] == chain[-1]["document_id"]
    assert chain[0]["pointer"] == chain[-1]["pointer"]


def test_cycle_not_involving_root_reports_closed_loop_only(client):
    _create(client, {"y": {"c": {"$ref": "doc:b@1#/x"}}}, doc_id="c")
    _create(client, {"x": {"b": {"$ref": "doc:c@1#/y"}}}, doc_id="b")
    _create(client, {"start": {"$ref": "doc:b@1#/x"}})
    r = _resolved(client)
    assert r.status_code == 409
    chain = r.json()["detail"]["chain"]
    # 环链从环的起点开始、以同一目标闭合，不含根文档
    assert [h["document_id"] for h in chain] == ["b", "c", "b"]
    assert chain[0]["pointer"] == chain[-1]["pointer"] == "/x"
    assert chain[1]["ref"] == "doc:c@1#/y"
    assert chain[1]["at"] == "/b"
    assert chain[-1]["ref"] == "doc:b@1#/x"
    assert chain[-1]["at"] == "/c"


def test_cycle_back_to_root_document_returns_409(client):
    _create(client, {"back": {"$ref": "doc:cfg@1#"}}, doc_id="dep")
    _create(client, {"v": {"$ref": "doc:dep@1#"}})
    r = _resolved(client)
    assert r.status_code == 409
    chain = r.json()["detail"]["chain"]
    assert [h["document_id"] for h in chain] == ["cfg", "dep", "cfg"]
    assert chain[0]["pointer"] == chain[-1]["pointer"] == ""


def test_diamond_is_not_a_cycle(client):
    # b 与 c 都引用 d：同一目标出现在不同分支，合法
    _create(client, {"leaf": 1}, doc_id="d")
    _create(client, {"v": {"$ref": "doc:d@1#/leaf"}}, doc_id="b")
    _create(client, {"v": {"$ref": "doc:d@1#/leaf"}}, doc_id="c")
    _create(
        client,
        {"l": {"$ref": "doc:b@1#/v"}, "r": {"$ref": "doc:c@1#/v"}},
    )
    r = _resolved(client)
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] == {"l": 1, "r": 1}


# ---------------- 最大解析深度 ----------------


def _build_deep_document(client, hops: int) -> None:
    """构造恰好 hops 跳的引用链：root → #/x0 → … → #/x<hops-1>（终点）。

    每一跳指针互不相同（/x0、/x1、…），不构成环，专门用于触达深度上限。
    """
    content = {f"x{hops - 1}": {"next": "end"}}
    for i in range(hops - 2, -1, -1):
        content[f"x{i}"] = {"next": {"$ref": f"doc:deep@1#/x{i + 1}"}}
    _create(client, content, doc_id="deep")
    _create(client, {"start": {"$ref": "doc:deep@1#/x0"}}, doc_id="root")


def test_depth_limit_32_hops_resolves(client):
    _build_deep_document(client, MAX_DEPTH)
    r = _resolved(client, doc_id="root")
    assert r.status_code == 200, r.text
    body = r.json()
    value = {"next": "end"}
    for _ in range(MAX_DEPTH - 1):
        value = {"next": value}
    assert body["resolved"] == {"start": value}
    assert len(body["dependencies"]) == MAX_DEPTH


def test_depth_33_hops_returns_422_with_chain(client):
    _build_deep_document(client, MAX_DEPTH + 1)
    r = _resolved(client, doc_id="root")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "max_depth_exceeded"
    assert detail["max_depth"] == MAX_DEPTH
    # 完整引用链：根 + 已解析的 32 跳 + 被拒绝的第 33 跳
    assert len(detail["chain"]) == MAX_DEPTH + 2
    assert detail["chain"][-1]["pointer"] == f"/x{MAX_DEPTH}"
