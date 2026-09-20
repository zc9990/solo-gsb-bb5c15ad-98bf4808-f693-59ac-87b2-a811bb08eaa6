"""写入租约（获取 / 续期 / 释放 / 查询 + PATCH 与原子发布门禁）集成测试。

每个用例使用独立临时 SQLite 文件；过期场景用亚秒级 TTL + 真实睡眠触发
（过期判断依赖服务端 UTC 单调前进，方向安全）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # db.connect() 每次读取 settings.db_path，直接改属性即可
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def _create(client, content=None, doc_id="cfg"):
    body = {"id": doc_id, "content": content if content is not None else {"a": 1}}
    r = client.post("/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _acquire(client, doc_id, holder, ttl=30):
    return client.post(
        f"/documents/{doc_id}/leases", json={"holder": holder, "ttl_seconds": ttl}
    )


def _patch(client, doc_id, base_revision, request_id, patch, lease_token=None):
    body = {"base_revision": base_revision, "request_id": request_id, "patch": patch}
    if lease_token is not None:
        body["lease_token"] = lease_token
    return client.patch(f"/documents/{doc_id}", json=body)


# ---------------- 获取 ----------------


def test_acquire_creates_lease_with_fencing_token(client):
    _create(client)
    r = _acquire(client, "cfg", "alice")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document_id"] == "cfg"
    assert body["holder"] == "alice"
    assert body["lease_token"] == 1
    assert body["reused"] is False
    assert body["ttl_seconds"] == 30
    assert body["acquired_at"] <= body["expires_at"]
    assert body["server_time"]


def test_acquire_same_holder_reuses_token_and_refreshes_expiry(client):
    _create(client)
    first = _acquire(client, "cfg", "alice", ttl=10).json()
    r = _acquire(client, "cfg", "alice", ttl=60)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reused"] is True
    assert body["lease_token"] == first["lease_token"]
    assert body["acquired_at"] == first["acquired_at"]
    # 过期时间按新 TTL 顺延（60s 必然晚于首次的 10s）
    assert body["expires_at"] > first["expires_at"]


def test_acquire_by_other_holder_conflicts(client):
    _create(client)
    _acquire(client, "cfg", "alice")
    r = _acquire(client, "cfg", "bob")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "lease_conflict"
    assert detail["holder"] == "alice"
    assert "expires_at" in detail


def test_fencing_token_strictly_increases_and_never_regresses(client):
    _create(client, doc_id="d1")
    _create(client, doc_id="d2")
    t1 = _acquire(client, "d1", "alice").json()["lease_token"]
    t2 = _acquire(client, "d2", "bob").json()["lease_token"]
    assert t2 > t1
    # 释放 d1 后重新获取：token 继续递增，不回退
    r = client.post(
        "/documents/d1/leases/release",
        json={"holder": "alice", "lease_token": t1},
    )
    assert r.json()["released"] is True
    t3 = _acquire(client, "d1", "alice").json()["lease_token"]
    assert t3 > t2


def test_expired_lease_takeover_assigns_new_token(client):
    _create(client)
    t1 = _acquire(client, "cfg", "alice", ttl=0.05).json()["lease_token"]
    time.sleep(0.15)
    r = _acquire(client, "cfg", "bob")
    assert r.status_code == 201, r.text
    assert r.json()["lease_token"] > t1
    assert r.json()["reused"] is False


def test_expired_lease_same_holder_reacquire_gets_new_token(client):
    # 旧租约过期即失效：即使同一 holder 重新获取也分配新 token
    _create(client)
    t1 = _acquire(client, "cfg", "alice", ttl=0.05).json()["lease_token"]
    time.sleep(0.15)
    r = _acquire(client, "cfg", "alice")
    assert r.status_code == 201
    assert r.json()["lease_token"] > t1


# ---------------- 续期 ----------------


def test_renew_extends_expiry_and_keeps_token(client):
    _create(client)
    first = _acquire(client, "cfg", "alice", ttl=10).json()
    token = first["lease_token"]
    r = client.post(
        "/documents/cfg/leases/renew",
        json={"holder": "alice", "lease_token": token, "ttl_seconds": 60},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lease_token"] == token
    assert body["acquired_at"] == first["acquired_at"]
    assert body["expires_at"] > first["expires_at"]


def test_renew_with_wrong_token_conflicts(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = client.post(
        "/documents/cfg/leases/renew",
        json={"holder": "alice", "lease_token": token + 100},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"


def test_renew_with_wrong_holder_conflicts(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = client.post(
        "/documents/cfg/leases/renew",
        json={"holder": "bob", "lease_token": token},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"


def test_renew_missing_lease_returns_404(client):
    _create(client)
    r = client.post(
        "/documents/cfg/leases/renew", json={"holder": "alice", "lease_token": 1}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "lease_not_found"


def test_renew_expired_lease_conflicts(client):
    _create(client)
    token = _acquire(client, "cfg", "alice", ttl=0.05).json()["lease_token"]
    time.sleep(0.15)
    r = client.post(
        "/documents/cfg/leases/renew", json={"holder": "alice", "lease_token": token}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"


# ---------------- 释放 ----------------


def test_release_deletes_lease(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = client.post(
        "/documents/cfg/leases/release",
        json={"holder": "alice", "lease_token": token},
    )
    assert r.status_code == 200
    assert r.json() == {"document_id": "cfg", "released": True}
    # 释放后查询不到租约
    assert client.get("/documents/cfg/leases").status_code == 404


def test_release_with_wrong_token_conflicts(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = client.post(
        "/documents/cfg/leases/release",
        json={"holder": "alice", "lease_token": token + 100},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"
    # 租约仍在
    assert client.get("/documents/cfg/leases").status_code == 200


def test_release_missing_lease_is_idempotent(client):
    _create(client)
    r = client.post(
        "/documents/cfg/leases/release", json={"holder": "alice", "lease_token": 1}
    )
    assert r.status_code == 200
    assert r.json() == {"document_id": "cfg", "released": False}


# ---------------- 查询 ----------------


def test_get_lease_returns_current_state(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = client.get("/documents/cfg/leases")
    assert r.status_code == 200
    body = r.json()
    assert body["holder"] == "alice"
    assert body["lease_token"] == token
    assert body["expired"] is False
    assert body["server_time"]


def test_get_lease_marks_expired(client):
    _create(client)
    _acquire(client, "cfg", "alice", ttl=0.05)
    time.sleep(0.15)
    r = client.get("/documents/cfg/leases")
    assert r.status_code == 200
    assert r.json()["expired"] is True


def test_get_lease_not_found(client):
    _create(client)
    r = client.get("/documents/cfg/leases")
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "lease_not_found"


def test_lease_endpoints_require_existing_document(client):
    assert _acquire(client, "ghost", "alice").status_code == 404
    assert client.get("/documents/ghost/leases").status_code == 404
    r = client.post(
        "/documents/ghost/leases/renew", json={"holder": "a", "lease_token": 1}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"
    r = client.post(
        "/documents/ghost/leases/release", json={"holder": "a", "lease_token": 1}
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "not_found"


# ---------------- PATCH 写入门禁 ----------------


def test_patch_with_matching_lease_token_succeeds(client):
    _create(client, {"a": 1, "b": 2})
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = _patch(client, "cfg", 1, "req-1", {"a": 9}, lease_token=token)
    assert r.status_code == 200, r.text
    assert r.json()["content"] == {"a": 9, "b": 2}
    assert client.get("/documents/cfg").json()["revision"] == 2


def test_patch_without_token_rejected_while_lease_active(client):
    _create(client)
    _acquire(client, "cfg", "alice")
    r = _patch(client, "cfg", 1, "req-1", {"a": 9})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "lease_conflict"
    assert detail["holder"] == "alice"
    assert detail["request_id"] == "req-1"
    # 未写入任何修订
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_patch_with_wrong_token_rejected(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    r = _patch(client, "cfg", 1, "req-1", {"a": 9}, lease_token=token + 100)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"
    assert client.get("/documents/cfg").json()["revision"] == 1


def test_lease_conflict_does_not_consume_request_id(client):
    _create(client)
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    # 错误 token 被拒绝
    r = _patch(client, "cfg", 1, "req-x", {"a": 9}, lease_token=token + 100)
    assert r.status_code == 409
    # 同一 request_id 修正 token 后重试成功（冲突未占用幂等 ID）
    r2 = _patch(client, "cfg", 1, "req-x", {"a": 9}, lease_token=token)
    assert r2.status_code == 200, r2.text
    assert r2.json()["revision"] == 2


def test_patch_without_lease_unchanged(client):
    # 原有无租约写入保持兼容：不带 token 照常工作
    _create(client, {"a": 1})
    r = _patch(client, "cfg", 1, "req-1", {"a": 2})
    assert r.status_code == 200
    assert r.json()["content"] == {"a": 2}


def test_patch_with_token_without_lease_rejected(client):
    # 无租约却携带 token：token 无从匹配，拒绝（防止误以为有租约保护）
    _create(client)
    r = _patch(client, "cfg", 1, "req-1", {"a": 2}, lease_token=1)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"


def test_expired_lease_old_token_permanently_invalid(client):
    _create(client, {"a": 1})
    token = _acquire(client, "cfg", "alice", ttl=0.05).json()["lease_token"]
    time.sleep(0.15)
    # 旧 token 永久失效：携带它的写入被拒绝
    r = _patch(client, "cfg", 1, "req-1", {"a": 2}, lease_token=token)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"
    # 租约过期后无租约裸写恢复兼容行为
    r2 = _patch(client, "cfg", 1, "req-2", {"a": 2})
    assert r2.status_code == 200
    assert r2.json()["content"] == {"a": 2}


def test_patch_after_release_without_token_succeeds(client):
    _create(client, {"a": 1})
    token = _acquire(client, "cfg", "alice").json()["lease_token"]
    client.post(
        "/documents/cfg/leases/release",
        json={"holder": "alice", "lease_token": token},
    )
    r = _patch(client, "cfg", 1, "req-1", {"a": 2})
    assert r.status_code == 200
    # 释放后旧 token 同样失效
    r2 = _patch(client, "cfg", 2, "req-2", {"a": 3}, lease_token=token)
    assert r2.status_code == 409


def test_revert_rejected_while_lease_active(client):
    _create(client, {"a": 1})
    assert _patch(client, "cfg", 1, "p1", {"a": 2}).status_code == 200
    _acquire(client, "cfg", "alice")
    r = client.post("/documents/cfg/reverts", json={"revision": 2, "request_id": "u1"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "lease_conflict"
    assert client.get("/documents/cfg").json()["revision"] == 2


# ---------------- 原子发布写入门禁 ----------------


def test_release_items_with_lease_token_succeed(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    token = _acquire(client, "d1", "alice").json()["lease_token"]
    r = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "updates": [
                {"document_id": "d1", "base_revision": 1, "patch": {"a": 2},
                 "lease_token": token},
                {"document_id": "d2", "base_revision": 1, "patch": {"b": 2}},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert [x["revision"] for x in r.json()["results"]] == [2, 2]


def test_release_item_lease_conflict_aggregated_and_release_id_reusable(client):
    _create(client, {"a": 1}, "d1")
    _create(client, {"b": 1}, "d2")
    token = _acquire(client, "d1", "alice").json()["lease_token"]
    r = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "updates": [
                # d1 有未过期租约但未携带 token
                {"document_id": "d1", "base_revision": 1, "patch": {"a": 2}},
                {"document_id": "d2", "base_revision": 1, "patch": {"b": 2}},
            ],
        },
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "release_failed"
    failure = detail["failures"][0]
    assert failure["document_id"] == "d1"
    assert failure["error"] == "lease_conflict"
    assert failure["status_code"] == 409
    # 整批回滚：d2 也没有产生修订
    assert client.get("/documents/d1").json()["revision"] == 1
    assert client.get("/documents/d2").json()["revision"] == 1
    # release_id 未被占用：修正后同 ID 重试成功
    r2 = client.post(
        "/releases",
        json={
            "release_id": "rel-1",
            "updates": [
                {"document_id": "d1", "base_revision": 1, "patch": {"a": 2},
                 "lease_token": token},
                {"document_id": "d2", "base_revision": 1, "patch": {"b": 2}},
            ],
        },
    )
    assert r2.status_code == 200, r2.text
    assert client.get("/documents/d1").json()["revision"] == 2
    assert client.get("/documents/d2").json()["revision"] == 2
