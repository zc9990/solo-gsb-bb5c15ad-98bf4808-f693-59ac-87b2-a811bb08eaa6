"""FastAPI 入口：文档创建 / 读取 / 修订历史 / 修订差异 / 字段归因 / 引用解析 / 变更影响预演 / 带三方合并的更新 / 写入租约。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from typing import Any, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse

from . import db
from .blame import RevisionSnapshot, compute_blame
from .config import settings
from .diff import OPS, snapshot_diff
from .guards import check_guards
from .merge import (
    apply_merge_patch,
    merge_patch_diff,
    present_conflict,
    three_way_merge,
)
from .resolve import ResolutionError, resolve_document
from .schemas import (
    AcquireLease,
    CreateDocument,
    CreateRelease,
    Guard,
    ImpactPreview,
    ReleaseLease,
    RenewLease,
    RevertDocument,
    SetContract,
    UpdateDocument,
)
from .validation import check_schema, validate_instance


@asynccontextmanager
async def lifespan(app: FastAPI) -> Iterator[None]:
    # 一键启动：确保目录存在并自动建表
    db.init_db()
    yield


app = FastAPI(
    title="共享 JSON 配置三方合并 API",
    version="1.8.0",
    description="基于 JSON Merge Patch (RFC 7386) 的共享配置存储；"
    "基线过期时递归三方合并，路径冲突返回 409；支持按修订号逆向撤销；"
    "支持任意两修订间的递归差异审计；"
    "支持历史字段归因（目标快照每个叶子值最后由哪次修订改变）；"
    "支持跨文档 $ref 引用解析（同一读事务快照内固定全部修订）；"
    "支持变更影响预演（不落库候选 + 各根文档当前/候选解析对比，只读）；"
    "支持更新前置条件 guards（写事务内先检查当前修订，未满足返回 409）；"
    "支持文档级临时写入租约（fencing token 全局严格递增，过期后旧 token 永久失效）。",
    lifespan=lifespan,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def err(status_code: int, code: str, **extra: Any) -> HTTPException:
    detail: dict[str, Any] = {"error": code}
    detail.update(extra)
    return HTTPException(status_code=status_code, detail=detail)


def row_content(row: sqlite3.Row) -> Any:
    return json.loads(row["content"])


# ---------------------------------------------------------------- 单项更新（共享）


class _ItemError(Exception):
    """单文档“基线→三方合并→契约校验”失败；调用方必须已在写事务内（失败即回滚）。

    单文档 PATCH 直接把它翻译成 HTTP 错误；原子发布则收集为结构化失败项，
    保证两条路径复用同一份合并与校验逻辑。
    """

    def __init__(self, code: str, status_code: int, **extra: Any) -> None:
        self.code = code
        self.status_code = status_code
        self.extra = extra
        super().__init__(code)


def _enforce_lease(
    conn: sqlite3.Connection,
    doc_id: str,
    lease_token: int | None,
    **ctx: Any,
) -> None:
    """写事务内按服务端 UTC 检查文档租约，失败抛 ``lease_conflict``（409）。

    * 存在**未过期**租约：仅当请求携带的 *lease_token* 等于当前 fencing token
      才放行；未携带或不匹配都拒绝；
    * 无未过期租约（无租约 / 已过期 / 已释放）：未携带 token 的请求放行
      （兼容原有无租约写入）；携带 token 的请求拒绝——旧 token 在租约过期
      或释放后即永久失效，避免"以为有租约保护、实际没有"的误写。

    失败由调用方回滚：不写修订、不占用幂等 ID（request_id / release_id）。
    """
    row = db.get_lease(conn, doc_id)
    now = db.utcnow()
    if row is not None and row["expires_at"] > now:
        if lease_token is not None and lease_token == row["fencing_token"]:
            return
        raise _ItemError(
            "lease_conflict",
            status.HTTP_409_CONFLICT,
            document_id=doc_id,
            holder=row["holder"],
            expires_at=row["expires_at"],
            reason=(
                "文档存在未过期租约，写入必须携带匹配的 lease_token"
                if lease_token is None
                else "lease_token 与当前租约的 fencing token 不匹配"
            ),
            **ctx,
        )
    if lease_token is not None:
        raise _ItemError(
            "lease_conflict",
            status.HTTP_409_CONFLICT,
            document_id=doc_id,
            reason="文档当前没有未过期租约，携带的 lease_token 已失效",
            **ctx,
        )


def _prepare_candidate(
    conn: sqlite3.Connection,
    doc_id: str,
    base_revision: int,
    patch: Any,
    request_id: str | None,
    *,
    lease_token: int | None = None,
    guards: list[Guard] | None = None,
    check_lease: bool = True,
    conflict_error: str = "conflict",
    conflict_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """执行「租约 → guards → 基线 → Merge Patch → 三方合并 → 契约校验」的只读流程。

    返回落库所需的候选结果，不写入任何数据，也不登记幂等记录；
    调用方负责插入修订 / 回滚。*conflict_error* / *conflict_extra* 允许撤销
    接口把同一种三方分歧渲染成自己的 ``revert_conflict`` 错误体。
    变更影响预演在同一**读事务**内复用本流程生成不落库候选：
    此时 *request_id* 为 None（错误体不含该字段）且 *check_lease* 为 False
    （预演只读，不检查也不改变租约）。

    顺序：
    ⓪ 文档存在未过期租约时，请求必须携带匹配的 *lease_token*
      （否则 ``lease_conflict`` 409；无有效租约时携带 token 同样拒绝；
      *check_lease* 为 False 时整步跳过）；
    ① 声明了 *guards* 时，按输入顺序逐条检查**当前修订**
      （任一未通过 → ``precondition_failed`` 409，列出全部未通过项）；
    ② 基线必须存在（``unknown_base_revision``）；
    ③ Merge Patch 应用到基线得到 theirs；基线过期时递归三方合并
      （路径分歧 → *conflict_error*）；
    ④ 文档存在契约时校验最终候选（``schema_validation_failed``）。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise _ItemError("not_found", status.HTTP_404_NOT_FOUND, document_id=doc_id)

    # 第〇步：写入租约——存在未过期租约时仅匹配当前 fencing token 的请求可写入。
    # 失败由调用方回滚：不写修订、不占用幂等 ID。只读预演跳过本步。
    if check_lease:
        _enforce_lease(
            conn, doc_id, lease_token, request_id=request_id, base_revision=base_revision
        )

    current_revision = doc["current_revision"]
    current_content = json.loads(
        db.get_revision(conn, doc_id, current_revision)["content"]
    )

    # 第①步：前置条件 guards——对当前修订按输入顺序求值，全部通过才进入
    # 基线检查与三方合并。失败由调用方回滚：不写修订、不占用幂等 ID。
    if guards:
        guard_failures = check_guards(current_content, guards)
        if guard_failures:
            raise _ItemError(
                "precondition_failed",
                status.HTTP_409_CONFLICT,
                document_id=doc_id,
                request_id=request_id,
                base_revision=base_revision,
                current_revision=current_revision,
                failures=guard_failures,
            )

    base_row = db.get_revision(conn, doc_id, base_revision)
    if base_row is None:
        raise _ItemError(
            "unknown_base_revision",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            document_id=doc_id,
            base_revision=base_revision,
            current_revision=doc["current_revision"],
            reason="base_revision 不存在",
        )

    base_content = json.loads(base_row["content"])

    # 第②步：把 Merge Patch 应用到基线上，得到“来稿想要的结果”
    theirs = apply_merge_patch(base_content, patch)

    if base_revision == current_revision:
        # 基线就是当前版：快路径，无需三方比较
        merged, replayed = theirs, False
    else:
        # 基线过期：递归比较 base / current / theirs
        merged, raw_conflicts = three_way_merge(base_content, current_content, theirs)
        replayed = True
        if raw_conflicts:
            detail: dict[str, Any] = {"document_id": doc_id}
            if request_id is not None:
                detail["request_id"] = request_id
            detail.update(
                {
                    "base_revision": base_revision,
                    "current_revision": current_revision,
                    "conflicts": [present_conflict(c) for c in raw_conflicts],
                }
            )
            if conflict_extra:
                detail = {**conflict_extra, **detail}
            raise _ItemError(conflict_error, status.HTTP_409_CONFLICT, **detail)

    # 第③步（Merge Patch 与三方重放均完成后）：若文档存在契约，
    # 校验最终候选内容。失败由调用方回滚——不写修订、不占用幂等 ID。
    contract_row = db.get_contract(conn, doc_id)
    if contract_row is not None:
        contract_schema = json.loads(contract_row["schema_json"])
        schema_errors = validate_instance(contract_schema, merged)
        if schema_errors:
            detail = {"document_id": doc_id}
            if request_id is not None:
                detail["request_id"] = request_id
            detail.update(
                {
                    "base_revision": base_revision,
                    "current_revision": current_revision,
                    "errors": schema_errors,
                }
            )
            raise _ItemError(
                "schema_validation_failed",
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                **detail,
            )

    changed = db.canonical(merged) != db.canonical(current_content)
    return {
        "doc": doc,
        "merged": merged,
        "replayed": replayed,
        "changed": changed,
        "current_revision": current_revision,
        "updated_at": db.utcnow() if changed else doc["updated_at"],
    }


def prepare_document_update(
    conn: sqlite3.Connection,
    doc_id: str,
    base_revision: int,
    patch: Any,
    request_id: str,
    guards: list[Guard] | None = None,
    lease_token: int | None = None,
) -> dict[str, Any]:
    """单文档 PATCH 的只读合并与校验；原子发布同样复用本入口。

    与 :func:`_prepare_candidate` 的顺序完全一致，路径分歧渲染为 ``conflict``；
    *lease_token* 用于写入租约门禁（存在未过期租约时必须匹配当前 fencing token）；
    *guards* 非空时先对当前修订求值，未通过渲染为 ``precondition_failed``。
    """
    return _prepare_candidate(
        conn,
        doc_id,
        base_revision,
        patch,
        request_id,
        lease_token=lease_token,
        guards=guards,
    )


def prepare_revert(
    conn: sqlite3.Connection,
    doc_id: str,
    target_revision: int,
    request_id: str,
) -> dict[str, Any]:
    """写事务内为撤销请求做只读准备，返回落库所需的候选结果。

    顺序：
    ① 文档必须存在（``not_found``，由路由在事务外先判一次）；
    ② 目标修订必须存在且 **大于 1**（修订 1 是创建快照，无可撤销的前态）
      → 否则 ``invalid_revert_revision`` 422；
    ③ 以目标修订的「前快照 revision-1 → 后快照 revision」构造逆向 Merge Patch；
    ④ 三方合并以**目标修订（后快照）为基线**、逆向补丁应用其上得到的前态为
      theirs、当前内容为 current——目标修订之后其他路径上的修改自动保留；
      目标涉及的路径后来被改成不同值 → ``revert_conflict`` 409
      （冲突体含 JSON Pointer 与三方值）；
    ⑤ 文档存在契约时，撤销后的最终候选仍须通过当前契约
      （``schema_validation_failed`` 422）。

    本函数不写入任何数据；冲突 / 校验失败由调用方回滚。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise _ItemError("not_found", status.HTTP_404_NOT_FOUND, document_id=doc_id)

    current_revision = doc["current_revision"]
    if target_revision <= 1 or target_revision > current_revision:
        raise _ItemError(
            "invalid_revert_revision",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            document_id=doc_id,
            revision=target_revision,
            current_revision=current_revision,
            reason="待撤销修订必须存在且大于 1（修订 1 为创建快照，无可撤销的前态）",
        )

    # 目标修订的行在上界检查后必然存在（修订号连续、不可删除）；
    # 前快照 revision-1 >= 1 同样必然存在。
    target_row = db.get_revision(conn, doc_id, target_revision)
    if target_row is None:  # 防御性分支：理论不可达
        raise _ItemError(
            "invalid_revert_revision",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            document_id=doc_id,
            revision=target_revision,
            current_revision=current_revision,
            reason="目标修订不存在",
        )
    before = json.loads(
        db.get_revision(conn, doc_id, target_revision - 1)["content"]
    )
    after = json.loads(target_row["content"])
    reverse_patch = merge_patch_diff(after, before)

    # 撤销方“站在目标修订上”：基线为后快照，逆向补丁应用到基线即得到前态。
    result = _prepare_candidate(
        conn,
        doc_id,
        target_revision,
        reverse_patch,
        request_id,
        conflict_error="revert_conflict",
        conflict_extra={"revert_revision": target_revision},
    )
    # 逆向补丁随候选结果带出，供路由落库到修订的 patch 列，避免重复构造。
    result["reverse_patch"] = reverse_patch
    result["reverted_revision"] = target_revision
    return result


# ---------------------------------------------------------------- 健康检查


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {"service": "json-config-merge", "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------- 创建


@app.post("/documents", status_code=status.HTTP_201_CREATED, tags=["documents"])
def create_document(
    body: CreateDocument, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc_id = body.id or hashlib.sha1(db.utcnow().encode()).hexdigest()[:16]
    now = db.utcnow()
    try:
        with db.transaction(conn):
            if db.get_document(conn, doc_id) is not None:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "document_exists",
                    document_id=doc_id,
                    reason="文档 ID 已存在",
                )
            db.insert_document(conn, doc_id, body.content, body.author, now)
    except HTTPException:
        raise
    return {
        "id": doc_id,
        "revision": 1,
        "content": body.content,
        "author": body.author,
        "created_at": now,
    }


# ---------------------------------------------------------------- 读取


@app.get("/documents", tags=["documents"])
def list_documents(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, current_revision, created_at, updated_at FROM documents"
        " ORDER BY id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@app.get("/documents/{doc_id}", tags=["documents"])
def get_document(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    rev = db.get_revision(conn, doc_id, doc["current_revision"])
    return {
        "id": doc_id,
        "revision": doc["current_revision"],
        "content": row_content(rev),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


@app.get("/documents/{doc_id}/revisions", tags=["documents"])
def get_revisions(
    doc_id: str,
    limit: int = Query(50, ge=1),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    limit = min(limit, settings.history_max_limit)
    rows = db.list_revisions(conn, doc_id, limit, offset, desc=(order == "desc"))
    items = [dict(r) for r in rows]
    for item in items:
        item["replayed"] = bool(item["replayed"])
    return {"document_id": doc_id, "items": items, "limit": limit, "offset": offset}


@app.get("/documents/{doc_id}/revisions/{revision}", tags=["documents"])
def get_one_revision(
    doc_id: str,
    revision: int,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_revision(conn, doc_id, revision)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "revision_not_found",
            document_id=doc_id,
            revision=revision,
        )
    return {
        "document_id": doc_id,
        "revision": row["revision"],
        "content": json.loads(row["content"]),
        "patch": json.loads(row["patch"]) if row["patch"] is not None else None,
        "base_revision": row["base_revision"],
        "request_id": row["request_id"],
        "replayed": bool(row["replayed"]),
        "author": row["author"],
        "created_at": row["created_at"],
    }


@app.get("/documents/{doc_id}/diff", tags=["documents"])
def diff_revisions(
    doc_id: str,
    from_revision: int = Query(..., ge=1, description="起始修订号（before 侧）"),
    to_revision: int = Query(..., ge=1, description="目标修订号（after 侧）"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """修订差异审计：递归比较两个已存在快照（只读，不写任何数据）。

    * 返回 add / remove / replace 变更，按 RFC 6901 路径稳定排序；
      每项含 before/after，``missing`` 区分键缺失与真实 null；
      数组作为整体值比较，新增 / 删除的子树不向下展开；
    * 任一修订不存在 → 404 revision_not_found，
      ``missing_revisions`` 指出缺失的修订号；
    * ``counts`` 汇总各操作计数；空差异返回空数组。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    from_row = db.get_revision(conn, doc_id, from_revision)
    to_row = db.get_revision(conn, doc_id, to_revision)
    # from == to 时去重；保持 from 在前、to 在后的指出顺序
    missing_revisions = list(
        dict.fromkeys(
            rev
            for rev, row in ((from_revision, from_row), (to_revision, to_row))
            if row is None
        )
    )
    if missing_revisions:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "revision_not_found",
            document_id=doc_id,
            from_revision=from_revision,
            to_revision=to_revision,
            missing_revisions=missing_revisions,
        )

    changes = snapshot_diff(
        json.loads(from_row["content"]), json.loads(to_row["content"])
    )
    counts = {op: 0 for op in OPS}
    for change in changes:
        counts[change["op"]] += 1
    return {
        "document_id": doc_id,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "changes": changes,
        "counts": {**counts, "total": len(changes)},
    }


# ---------------------------------------------------------------- 历史字段归因


@app.get("/documents/{doc_id}/blame", tags=["documents"])
def blame_document(
    doc_id: str,
    revision: int = Query(..., ge=1, description="目标修订号（归因该快照的叶子值）"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """历史字段归因：目标快照中每个叶子值最后由哪次修订改变（只读，不写任何数据）。

    * 在**同一 SQLite 读事务**内从修订 1 起按修订顺序比较相邻快照：
      对象递归到叶子，数组 / 标量 / null / 空对象整体视为叶子；
    * 叶子路径新出现或值变化 → 归属于该修订；路径删除后再次出现时
      归属于**重建它**的修订，即使值与删除前相同；
    * 每项含 ``path``（RFC 6901）、``value``、``last_changed_revision``、
      ``author``、``request_id``、``created_at``，按 ``path`` 稳定排序；
    * 目标修订不存在 → ``404 revision_not_found``。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    # 只读事务（WAL 快照）：本次归因见到的全部修订相互一致，且不产生任何写入
    with db.read_transaction(conn):
        if db.get_revision(conn, doc_id, revision) is None:
            raise err(
                status.HTTP_404_NOT_FOUND,
                "revision_not_found",
                document_id=doc_id,
                revision=revision,
            )
        rows = db.list_revisions_through(conn, doc_id, revision)
        entries = compute_blame(
            [
                RevisionSnapshot(
                    revision=row["revision"],
                    content=json.loads(row["content"]),
                    author=row["author"],
                    request_id=row["request_id"],
                    created_at=row["created_at"],
                )
                for row in rows
            ]
        )

    return {
        "document_id": doc_id,
        "revision": revision,
        "entries": entries,
    }


# ---------------------------------------------------------------- 跨文档引用解析


def _current_revision_or_none(conn: sqlite3.Connection, doc_id: str) -> int | None:
    row = db.get_document(conn, doc_id)
    return row["current_revision"] if row is not None else None


def _revision_content_or_none(
    conn: sqlite3.Connection, doc_id: str, revision: int
) -> Any:
    row = db.get_revision(conn, doc_id, revision)
    return json.loads(row["content"]) if row is not None else None


@app.get("/documents/{doc_id}/resolved", tags=["documents"])
def get_resolved_document(
    doc_id: str,
    revision: str = Query(
        "current", description="根文档修订：正整数修订号或 current（默认）"
    ),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """跨文档引用解析：递归展开 ``$ref`` 引用，返回拼装后的完整文档（只读）。

    * 仅含 ``$ref`` 键且值形如 ``doc:<ID>@<正整数|current>#<RFC6901指针>``
      的对象视为引用，在对象与数组中递归替换；其余 ``$ref`` 对象原样保留；
    * 全程在**同一 SQLite 读事务快照**内：根文档与所有 ``current`` 引用的
      修订在快照内固定，并发写入不影响本次解析结果；
    * 返回 ``resolved``（解析结果）、``revision``（根修订）与
      ``dependencies``（按首次遍历排序、按 文档+修订+指针 去重的依赖清单）；
    * 文档 / 修订 / 指针不存在 → ``422`` 及完整引用链；
      依赖成环 → ``409`` 及首尾闭合链；最大解析深度 32。
    """
    if revision == "current":
        revision_spec: int | str = "current"
    elif re.fullmatch(r"[1-9][0-9]*", revision):
        revision_spec = int(revision)
    else:
        raise err(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_revision",
            document_id=doc_id,
            revision=revision,
            reason="revision 必须是正整数修订号或 current",
        )

    # 只读事务（WAL 快照）：本次解析见到的所有修订相互一致
    with db.read_transaction(conn):
        try:
            resolved, root_revision, dependencies = resolve_document(
                doc_id,
                revision_spec,
                lambda target: _current_revision_or_none(conn, target),
                lambda target, rev: _revision_content_or_none(conn, target, rev),
            )
        except ResolutionError as exc:
            raise err(exc.status_code, exc.code, chain=exc.chain, **exc.extra)

    return {
        "id": doc_id,
        "revision": root_revision,
        "resolved": resolved,
        "dependencies": dependencies,
    }


# ---------------------------------------------------------------- 变更影响预演


def _union_dependencies(
    first: list[dict[str, Any]], second: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """合并两遍解析的依赖清单：保持首次遍历顺序（当前解析在前），
    按 (文档, 修订, 指针) 去重。"""
    merged = list(first)
    seen = {(d["document_id"], d["revision"], d["pointer"]) for d in first}
    for dep in second:
        key = (dep["document_id"], dep["revision"], dep["pointer"])
        if key not in seen:
            seen.add(key)
            merged.append(dep)
    return merged


def _preview_root_error(
    root_id: str, phase: str, exc: ResolutionError
) -> dict[str, Any]:
    """把单个根的解析失败整理成结果项（不影响其他根）。

    *phase* 指出失败发生在哪一遍：``current``（当前内容已无法解析，
    预演基线缺失）或 ``candidate``（候选引入的解析失败）。
    """
    return {
        "root_document_id": root_id,
        "phase": phase,
        "error": exc.code,
        "status_code": exc.status_code,
        **exc.extra,
        "chain": exc.chain,
    }


def _impact_preview_root(
    conn: sqlite3.Connection,
    target_id: str,
    root_id: str,
    candidate: Any,
    changed: bool,
    candidate_revision: int,
) -> dict[str, Any]:
    """对单个根文档做「当前内容 / 候选覆盖目标文档」两遍 $ref 解析并给出差异。

    候选解析中目标文档的当前修订呈现为 *candidate_revision*（候选若落库将占用
    的修订号）：``@current`` 引用读到候选内容，``@<历史修订>`` 的固定引用始终
    读历史快照。任一遍失败 → 返回携带完整引用链的错误项，不影响其他根。
    """
    try:
        resolved_current, root_revision, deps_current = resolve_document(
            root_id,
            "current",
            lambda target: _current_revision_or_none(conn, target),
            lambda target, rev: _revision_content_or_none(conn, target, rev),
        )
    except ResolutionError as exc:
        return _preview_root_error(root_id, "current", exc)

    if not changed:
        # 候选与当前内容一致：两遍解析必然相同，无需第二遍
        return {
            "root_document_id": root_id,
            "revision": root_revision,
            "dependencies": deps_current,
            "resolved_changes": [],
        }

    def pinned_current(target: str) -> int | None:
        # 候选视为目标文档的下一个修订：@current 引用在候选解析中固定到它
        if target == target_id:
            return candidate_revision
        return _current_revision_or_none(conn, target)

    def content_or_candidate(target: str, revision: int) -> Any:
        if target == target_id and revision == candidate_revision:
            return candidate
        return _revision_content_or_none(conn, target, revision)

    try:
        resolved_candidate, _, deps_candidate = resolve_document(
            root_id, "current", pinned_current, content_or_candidate
        )
    except ResolutionError as exc:
        return _preview_root_error(root_id, "candidate", exc)

    return {
        "root_document_id": root_id,
        "revision": root_revision,
        "dependencies": _union_dependencies(deps_current, deps_candidate),
        "resolved_changes": snapshot_diff(resolved_current, resolved_candidate),
    }


@app.post("/documents/{doc_id}/impact-preview", tags=["documents"])
def impact_preview(
    doc_id: str,
    body: ImpactPreview,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """变更影响预演：只读地预演一次 PATCH 对各根文档解析视图的影响。

    * 在**同一个 SQLite 读事务快照**内：先按现有三方合并与契约规则生成
      **不落库候选**（与 PATCH 相同的基线检查 / 三方重放 / 契约校验），
      再分别以当前内容和候选覆盖目标文档，递归解析每个根文档的 ``$ref``；
    * 候选冲突 / 契约失败 / 基线无效 / 文档不存在 → 整体返回与 PATCH
      相同的结构化错误（无 ``request_id`` 字段），不返回任何根结果；
    * 每个根返回固定修订、两遍解析的依赖并集与按差异审计语义排序的
      ``resolved_changes``；单个根解析失败仅在该根结果中记录引用链错误，
      不影响其他根；重复根按首次出现去重；
    * 预演为只读分析：**不创建修订、不占用 request_id、不改变租约**
      （也不检查租约与 guards）。
    """
    # 重复根按首次出现去重
    root_ids = list(dict.fromkeys(body.root_document_ids))

    # 只读事务（WAL 快照）：候选生成与全部根的两遍解析看到一致的内容，
    # 且不产生任何写入（不创建修订、不占用 request_id、不改变租约）。
    with db.read_transaction(conn):
        try:
            result = _prepare_candidate(
                conn,
                doc_id,
                body.base_revision,
                body.patch,
                request_id=None,
                check_lease=False,
            )
        except _ItemError as exc:
            raise err(exc.status_code, exc.code, **exc.extra)

        candidate = result["merged"]
        changed = result["changed"]
        current_revision = result["current_revision"]
        # 候选若落库将占用的修订号（与 PATCH 的 changed 语义一致）；
        # 候选覆盖解析时，目标文档的 @current 引用固定到该虚拟修订。
        candidate_revision = current_revision + 1 if changed else current_revision

        results = [
            _impact_preview_root(
                conn, doc_id, root_id, candidate, changed, candidate_revision
            )
            for root_id in root_ids
        ]

    return {
        "id": doc_id,
        "base_revision": body.base_revision,
        "current_revision": current_revision,
        "candidate_revision": candidate_revision,
        "replayed": result["replayed"],
        "changed": changed,
        "candidate": candidate,
        "results": results,
    }


# ---------------------------------------------------------------- 契约（JSON Schema 2020-12）


@app.put("/documents/{doc_id}/schema", tags=["contracts"])
def put_contract(
    doc_id: str,
    body: SetContract,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """设置 / 替换文档契约。

    顺序：① 元校验 Schema 本身；② 在同一写事务内校验**当前文档**实例。
    任一步不通过都返回 422 且**不替换**既有契约（无契约时也不创建）。
    """
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    schema = body.schema_
    schema_errors = check_schema(schema)
    if schema_errors:
        raise err(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "invalid_schema",
            document_id=doc_id,
            reason="schema 本身不符合 JSON Schema 2020-12",
            errors=schema_errors,
        )

    with db.transaction(conn):
        # 事务内重读当前文档与既有契约：验证与替换原子完成，
        # 不与并发 PATCH 产生“按旧内容验证、按新内容替换”的窗口。
        doc = db.get_document(conn, doc_id)
        current_content = json.loads(
            db.get_revision(conn, doc_id, doc["current_revision"])["content"]
        )
        instance_errors = validate_instance(schema, current_content)
        if instance_errors:
            raise err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "schema_validation_failed",
                document_id=doc_id,
                revision=doc["current_revision"],
                reason="当前文档不满足所给 schema，契约未替换",
                errors=instance_errors,
            )
        now = db.utcnow()
        db.upsert_contract(conn, doc_id, schema, now)

    return {
        "document_id": doc_id,
        "schema": schema,
        "revision": doc["current_revision"],
        "updated_at": now,
    }


@app.get("/documents/{doc_id}/schema", tags=["contracts"])
def get_contract(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_contract(conn, doc_id)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "schema_not_found",
            document_id=doc_id,
            reason="该文档未设置契约",
        )
    return {
        "document_id": doc_id,
        "schema": json.loads(row["schema_json"]),
        "updated_at": row["updated_at"],
    }


@app.delete("/documents/{doc_id}/schema", tags=["contracts"])
def delete_contract(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """删除契约；删除后 PATCH 恢复为无契约的原更新行为。幂等。"""
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    with db.transaction(conn):
        existed = db.get_contract(conn, doc_id) is not None
        db.delete_contract(conn, doc_id)
    return {"document_id": doc_id, "deleted": existed}


# ---------------------------------------------------------------- 更新（三方合并）


def _guards_fingerprint(guards: list[Guard] | None) -> Any:
    """guards 的幂等指纹形态；未声明（或空数组）时返回 None。

    返回 None 时调用方不在指纹中追加 guards 段，从而保持无 guards 请求的
    指纹格式与历史幂等记录一致；空数组与省略 guards 语义相同、指纹相同。
    """
    if not guards:
        return None
    return [g.fingerprint_form() for g in guards]


@app.patch("/documents/{doc_id}", tags=["documents"])
def update_document(
    doc_id: str,
    body: UpdateDocument,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    doc = db.get_document(conn, doc_id)
    if doc is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    # 幂等指纹：request_id 相同且 (base_revision, patch, guards) 相同 → 同一请求；
    # 无 guards 时不追加第三段，与历史幂等记录的指纹格式保持一致。
    guards_form = _guards_fingerprint(body.guards)
    fingerprint_src = f"{body.base_revision}\n{db.canonical(body.patch)}"
    if guards_form is not None:
        fingerprint_src += f"\n{db.canonical(guards_form)}"
    fingerprint = hashlib.sha256(fingerprint_src.encode("utf-8")).hexdigest()

    existing = db.get_request(conn, doc_id, body.request_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "request_id_conflict",
                document_id=doc_id,
                request_id=body.request_id,
                reason="同一 request_id 已用于 base_revision/patch/guards 不同的请求",
            )
        rev_row = db.get_revision(conn, doc_id, existing["revision"])
        return JSONResponse(
            {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(rev_row["content"]),
                "created_at": rev_row["created_at"],
            }
        )

    # 写事务内重读，消除“检查后写入”的并发窗口（BEGIN IMMEDIATE 取写锁）
    with db.transaction(conn):
        doc = db.get_document(conn, doc_id)
        existing = db.get_request(conn, doc_id, body.request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "request_id_conflict",
                    document_id=doc_id,
                    request_id=body.request_id,
                    reason="同一 request_id 已用于 base_revision/patch/guards 不同的请求",
                )
            current_row = db.get_revision(conn, doc_id, existing["revision"])
            payload = {
                "id": doc_id,
                "revision": existing["revision"],
                "base_revision": body.base_revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(current_row["content"]),
                "created_at": current_row["created_at"],
            }
            return JSONResponse(payload)

        try:
            result = prepare_document_update(
                conn,
                doc_id,
                body.base_revision,
                body.patch,
                body.request_id,
                guards=body.guards,
                lease_token=body.lease_token,
            )
        except _ItemError as exc:
            raise err(exc.status_code, exc.code, **exc.extra)

        merged = result["merged"]
        replayed = result["replayed"]
        changed = result["changed"]
        current_revision = result["current_revision"]
        now = db.utcnow()
        if changed:
            new_revision = current_revision + 1
            db.insert_revision(
                conn,
                doc_id,
                new_revision,
                merged,
                body.patch,
                body.base_revision,
                body.request_id,
                replayed,
                body.author,
                now,
            )
        else:
            # 自动重放后结果与当前版相同：不生成新修订
            new_revision = current_revision

        db.insert_request(
            conn, doc_id, body.request_id, fingerprint, new_revision, changed
        )

    return JSONResponse(
        {
            "id": doc_id,
            "revision": new_revision,
            "base_revision": body.base_revision,
            "request_id": body.request_id,
            "replayed": replayed,
            "changed": changed,
            "idempotent": False,
            "content": merged,
            "created_at": now if changed else result["updated_at"],
        }
    )


# ---------------------------------------------------------------- 撤销（逆向三方合并）


@app.post("/documents/{doc_id}/reverts", tags=["documents"])
def revert_document(
    doc_id: str,
    body: RevertDocument,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """撤销指定修订：以其前后快照构造逆向变更，再与当前内容三方合并。

    * 目标修订必须存在且大于 1 → 否则 ``422 invalid_revert_revision``；
    * 目标修订之后发生在**其他路径**的修改自动保留；
    * 目标涉及的路径后来被改成不同值 → ``409 revert_conflict``，
      冲突体含 JSON Pointer 与 base/current/theirs 三方值，事务回滚不写新修订；
    * 撤销成功生成一条**普通修订**（可在历史中追溯被撤销的修订号与 request_id），
      最终候选仍须通过文档当前的 JSON Schema 契约；
    * ``request_id`` 幂等：相同请求（revision/author 规范化指纹相同）重放原结果，
      同 ID 不同内容 → ``409 request_id_conflict``。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    # 幂等指纹：revert 前缀与 PATCH 的 (base_revision, patch) 指纹区分命名空间，
    # 内容包含 revision 与 author，任一变化都视为不同请求。
    fingerprint = hashlib.sha256(
        b"revert\n"
        + db.canonical([body.revision, body.author]).encode("utf-8")
    ).hexdigest()

    existing = db.get_request(conn, doc_id, body.request_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "request_id_conflict",
                document_id=doc_id,
                request_id=body.request_id,
                reason="同一 request_id 已用于 revision/author 不同（或非撤销）的请求",
            )
        rev_row = db.get_revision(conn, doc_id, existing["revision"])
        return JSONResponse(
            {
                "id": doc_id,
                "revision": existing["revision"],
                "reverted_revision": body.revision,
                "request_id": body.request_id,
                "replayed": None,
                "changed": bool(existing["changed"]),
                "idempotent": True,
                "content": json.loads(rev_row["content"]),
                "created_at": rev_row["created_at"],
            }
        )

    # 写事务内重读，消除“检查后写入”的并发窗口（BEGIN IMMEDIATE 取写锁）
    with db.transaction(conn):
        doc = db.get_document(conn, doc_id)
        if doc is None:
            raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
        existing = db.get_request(conn, doc_id, body.request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "request_id_conflict",
                    document_id=doc_id,
                    request_id=body.request_id,
                    reason="同一 request_id 已用于 revision/author 不同（或非撤销）的请求",
                )
            current_row = db.get_revision(conn, doc_id, existing["revision"])
            return JSONResponse(
                {
                    "id": doc_id,
                    "revision": existing["revision"],
                    "reverted_revision": body.revision,
                    "request_id": body.request_id,
                    "replayed": None,
                    "changed": bool(existing["changed"]),
                    "idempotent": True,
                    "content": json.loads(current_row["content"]),
                    "created_at": current_row["created_at"],
                }
            )

        try:
            result = prepare_revert(conn, doc_id, body.revision, body.request_id)
        except _ItemError as exc:
            raise err(exc.status_code, exc.code, **exc.extra)

        merged = result["merged"]
        replayed = result["replayed"]
        changed = result["changed"]
        current_revision = result["current_revision"]
        # 落库修订的 base_revision 记录为被撤销的目标修订；
        # patch 列为逆向 Merge Patch，历史可完整追溯。
        reverse_patch = result["reverse_patch"]
        now = db.utcnow()
        if changed:
            new_revision = current_revision + 1
            db.insert_revision(
                conn,
                doc_id,
                new_revision,
                merged,
                reverse_patch,
                body.revision,
                body.request_id,
                replayed,
                body.author,
                now,
            )
        else:
            # 逆向三方合并后结果与当前版相同（撤销目标的影响早已不存在）：
            # 与 PATCH 一致，不生成新修订。
            new_revision = current_revision

        db.insert_request(
            conn, doc_id, body.request_id, fingerprint, new_revision, changed
        )

    return JSONResponse(
        {
            "id": doc_id,
            "revision": new_revision,
            "reverted_revision": body.revision,
            "request_id": body.request_id,
            "replayed": replayed,
            "changed": changed,
            "idempotent": False,
            "content": merged,
            "created_at": now if changed else result["updated_at"],
        }
    )


# ---------------------------------------------------------------- 原子发布


def _release_failure(index: int, document_id: str, exc: _ItemError) -> dict[str, Any]:
    """把单项失败整理成按输入顺序汇总的结构化失败项。"""
    failure: dict[str, Any] = {
        "index": index,
        "document_id": document_id,
        "error": exc.code,
        "status_code": exc.status_code,
    }
    failure.update(exc.extra)
    return failure


@app.post("/releases", tags=["releases"])
def create_release(
    body: CreateRelease, conn: sqlite3.Connection = Depends(get_conn)
) -> JSONResponse:
    """原子发布：在同一 SQLite 写事务内为多个文档各做一次三方合并 + 契约校验。

    * 更新项可携带 ``guards`` 前置条件（对当前修订求值，先于合并）；
    * 更新项可携带 ``lease_token``：目标文档存在未过期租约时必须匹配当前
      fencing token，否则该项以 ``lease_conflict``（409）聚合进失败列表；
    * 任一文档不存在 / guards 未通过 / 基线无效 / 合并冲突 / 契约校验失败
      → 422 release_failed，``failures`` 按输入顺序汇总，全部文档不产生修订，
      且 release_id 不被占用；
    * 全部通过 → 一次性写入各文档新修订，返回每项最终 revision/content；
    * release_id 对**规范化整批请求**持久化幂等：
      相同请求（含顺序、author 与 guards）重放原结果，不同请求 409 release_id_conflict。
    """
    # 规范化指纹：按输入顺序记录每项 (document_id, base_revision, patch, author)，
    # 紧凑且键排序的确定性 JSON；字段顺序/空白变化不影响语义。
    # 任一项声明 guards 时，每项追加其 guards 指纹形态（未声明的项为 null）；
    # 整批都无 guards 时不追加，与历史幂等记录的指纹格式保持一致。
    any_guards = any(item.guards for item in body.updates)
    fingerprint = hashlib.sha256(
        db.canonical(
            [
                [item.document_id, item.base_revision, item.patch, item.author]
                + ([_guards_fingerprint(item.guards)] if any_guards else [])
                for item in body.updates
            ]
        ).encode("utf-8")
    ).hexdigest()

    existing = db.get_release(conn, body.release_id)
    if existing is not None:
        if existing["fingerprint"] != fingerprint:
            raise err(
                status.HTTP_409_CONFLICT,
                "release_id_conflict",
                release_id=body.release_id,
                reason="同一 release_id 已用于 updates 不同的发布请求",
            )
        replay = json.loads(existing["results"])
        replay["idempotent"] = True
        return JSONResponse(replay)

    # 单一写事务（BEGIN IMMEDIATE）：全部通过才提交，任一失败整体回滚。
    with db.transaction(conn):
        # 事务内重读幂等记录，消除与并发发布之间的竞态窗口。
        existing = db.get_release(conn, body.release_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "release_id_conflict",
                    release_id=body.release_id,
                    reason="同一 release_id 已用于 updates 不同的发布请求",
                )
            replay = json.loads(existing["results"])
            replay["idempotent"] = True
            return JSONResponse(replay)

        # 逐项复用单文档 PATCH 的合并与契约校验逻辑，收集**全部**失败项。
        failures: list[dict[str, Any]] = []
        prepared: list[tuple[int, Any, dict[str, Any]]] = []
        for index, item in enumerate(body.updates):
            try:
                prepared.append(
                    (
                        index,
                        item,
                        prepare_document_update(
                            conn,
                            item.document_id,
                            item.base_revision,
                            item.patch,
                            body.release_id,
                            guards=item.guards,
                            lease_token=item.lease_token,
                        ),
                    )
                )
            except _ItemError as exc:
                failures.append(_release_failure(index, item.document_id, exc))

        if failures:
            # 不捕获：db.transaction 回滚，修订与 release_id 均不落库。
            raise err(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "release_failed",
                release_id=body.release_id,
                reason="发布中存在失败项，全部文档未产生修订",
                failures=failures,
            )

        # 全部通过：按输入顺序一次性写入各文档新修订。
        # 发布内 document_id 已在模型层保证互不重复，故逐项 +1 互不影响。
        now = db.utcnow()
        results: list[dict[str, Any]] = []
        for index, item, result in prepared:
            current_revision = result["current_revision"]
            merged = result["merged"]
            changed = result["changed"]
            if changed:
                new_revision = current_revision + 1
                db.insert_revision(
                    conn,
                    item.document_id,
                    new_revision,
                    merged,
                    item.patch,
                    item.base_revision,
                    body.release_id,
                    result["replayed"],
                    item.author,
                    now,
                )
                created_at = now
            else:
                # 自动重放后结果与当前版相同：不生成新修订
                new_revision = current_revision
                created_at = result["updated_at"]
            results.append(
                {
                    "document_id": item.document_id,
                    "revision": new_revision,
                    "base_revision": item.base_revision,
                    "replayed": result["replayed"],
                    "changed": changed,
                    "author": item.author,
                    "content": merged,
                    "created_at": created_at,
                }
            )

        payload: dict[str, Any] = {
            "release_id": body.release_id,
            "idempotent": False,
            "results": results,
        }
        # 仅持久化成功发布；失败路径在上面已抛出并回滚，不占用 release_id。
        db.insert_release(conn, body.release_id, fingerprint, payload, now)

    return JSONResponse(payload)


# ---------------------------------------------------------------- 写入租约


@app.post("/documents/{doc_id}/leases", tags=["leases"])
def acquire_lease(
    doc_id: str,
    body: AcquireLease,
    conn: sqlite3.Connection = Depends(get_conn),
) -> JSONResponse:
    """获取文档的临时写入租约（写事务内按服务端 UTC 判断过期）。

    * 无租约或租约已过期 → 新持有者接管，分配**全局严格递增**的 fencing
      token（响应字段 ``lease_token``），返回 201；旧 token 自此永久失效；
    * 同一 holder 对**未过期**租约重复获取 → 沿用当前 token，仅按新的
      ``ttl_seconds`` 刷新过期时间，返回 200（``reused: true``）；
    * 其他 holder 持有未过期租约 → 409 ``lease_conflict``。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    with db.transaction(conn):
        now = db.utcnow()
        row = db.get_lease(conn, doc_id)
        if row is not None and row["expires_at"] > now:
            if row["holder"] != body.holder:
                raise err(
                    status.HTTP_409_CONFLICT,
                    "lease_conflict",
                    document_id=doc_id,
                    holder=row["holder"],
                    expires_at=row["expires_at"],
                    reason="文档存在其他持有者的未过期租约",
                )
            # 同一 holder 重复获取有效租约：沿用当前 token，刷新过期时间
            expires_at = db.utc_after(body.ttl_seconds)
            db.update_lease_expiry(conn, doc_id, expires_at)
            return JSONResponse(
                {
                    "document_id": doc_id,
                    "holder": row["holder"],
                    "lease_token": row["fencing_token"],
                    "ttl_seconds": body.ttl_seconds,
                    "acquired_at": row["acquired_at"],
                    "expires_at": expires_at,
                    "server_time": now,
                    "reused": True,
                }
            )

        # 无租约或租约已过期：新持有者接管，分配严格递增的 fencing token
        token = db.next_fencing_token(conn)
        expires_at = db.utc_after(body.ttl_seconds)
        db.upsert_lease(conn, doc_id, body.holder, token, now, expires_at)
        return JSONResponse(
            {
                "document_id": doc_id,
                "holder": body.holder,
                "lease_token": token,
                "ttl_seconds": body.ttl_seconds,
                "acquired_at": now,
                "expires_at": expires_at,
                "server_time": now,
                "reused": False,
            },
            status_code=status.HTTP_201_CREATED,
        )


@app.post("/documents/{doc_id}/leases/renew", tags=["leases"])
def renew_lease(
    doc_id: str,
    body: RenewLease,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """续期租约：holder 与 lease_token 必须匹配当前**未过期**租约。

    沿用当前 fencing token，仅把过期时间顺延为服务端 UTC 当前时间 +
    ``ttl_seconds``；租约不存在 → 404 ``lease_not_found``，已过期 →
    409 ``lease_conflict``（旧 token 已永久失效，需重新获取）。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    with db.transaction(conn):
        now = db.utcnow()
        row = db.get_lease(conn, doc_id)
        if row is None:
            raise err(
                status.HTTP_404_NOT_FOUND,
                "lease_not_found",
                document_id=doc_id,
                reason="文档当前没有租约",
            )
        if row["expires_at"] <= now:
            raise err(
                status.HTTP_409_CONFLICT,
                "lease_conflict",
                document_id=doc_id,
                holder=row["holder"],
                expires_at=row["expires_at"],
                reason="租约已过期，旧 lease_token 已永久失效，请重新获取",
            )
        if row["holder"] != body.holder or row["fencing_token"] != body.lease_token:
            raise err(
                status.HTTP_409_CONFLICT,
                "lease_conflict",
                document_id=doc_id,
                holder=row["holder"],
                expires_at=row["expires_at"],
                reason="holder 或 lease_token 与当前租约不匹配",
            )
        expires_at = db.utc_after(body.ttl_seconds)
        db.update_lease_expiry(conn, doc_id, expires_at)
        return {
            "document_id": doc_id,
            "holder": row["holder"],
            "lease_token": row["fencing_token"],
            "ttl_seconds": body.ttl_seconds,
            "acquired_at": row["acquired_at"],
            "expires_at": expires_at,
            "server_time": now,
        }


@app.post("/documents/{doc_id}/leases/release", tags=["leases"])
def release_lease(
    doc_id: str,
    body: ReleaseLease,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """释放租约：holder 与 lease_token 匹配当前租约才删除。

    释放后文档回到无租约状态（写入恢复原有行为）；租约不存在时为幂等
    no-op（``released: false``）；凭证不匹配 → 409 ``lease_conflict``。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)

    with db.transaction(conn):
        row = db.get_lease(conn, doc_id)
        if row is None:
            return {"document_id": doc_id, "released": False}
        if row["holder"] != body.holder or row["fencing_token"] != body.lease_token:
            raise err(
                status.HTTP_409_CONFLICT,
                "lease_conflict",
                document_id=doc_id,
                holder=row["holder"],
                reason="holder 或 lease_token 与当前租约不匹配",
            )
        db.delete_lease(conn, doc_id)
        return {"document_id": doc_id, "released": True}


@app.get("/documents/{doc_id}/leases", tags=["leases"])
def get_current_lease(
    doc_id: str, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    """查询文档当前租约（只读）：``expired`` 按服务端 UTC 当场计算。

    过期租约在被接管或释放前仍可查到（``expired: true``）；
    从未租约或已释放 → 404 ``lease_not_found``。
    """
    if db.get_document(conn, doc_id) is None:
        raise err(status.HTTP_404_NOT_FOUND, "not_found", document_id=doc_id)
    row = db.get_lease(conn, doc_id)
    if row is None:
        raise err(
            status.HTTP_404_NOT_FOUND,
            "lease_not_found",
            document_id=doc_id,
            reason="文档当前没有租约",
        )
    now = db.utcnow()
    return {
        "document_id": doc_id,
        "holder": row["holder"],
        "lease_token": row["fencing_token"],
        "acquired_at": row["acquired_at"],
        "expires_at": row["expires_at"],
        "server_time": now,
        "expired": row["expires_at"] <= now,
    }
