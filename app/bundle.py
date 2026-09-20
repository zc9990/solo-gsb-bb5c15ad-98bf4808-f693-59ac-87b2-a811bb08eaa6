"""跨文档依赖锁定包：把一组根文档及其 ``$ref`` 依赖闭包固定为可复现的快照包。

与 :mod:`.resolve` 的“解析视图”不同，锁定包**不做引用替换**，而是把整棵
依赖图收集为可持久化、可比对的三部分：

* **改写后根列表**：根文档的 ``current`` 固定为当次读事务快照所见的数字
  修订，数字修订保持不变；按 (文档, 修订) 去重并排序；
* **原始文档快照**：闭包内每个 (文档, 修订) 节点的完整原始内容
  （引用保持 ``$ref`` 形态，不展开）；
* **依赖边**：每个快照内符合现有引用语法
  （``doc:<ID>@<正整数|current>#<RFC6901指针>``）的 ``$ref`` 各产生一条边，
  边的目标修订同样把 ``@current`` 固定为数字修订。

闭包按**整篇快照**遍历：一个 (文档, 修订) 进入闭包后，其内容里所有符合
引用语法的 ``$ref`` 都会被校验（文档 / 修订 / 指针必须存在）并跟随，
目标节点随之进入闭包；按 (文档, 修订) 去重使遍历在有限步内终止，
因此依赖成环只是闭包中的普通边，不像解析那样报错。

三部分输出均按 (文档, 修订, 指针) 稳定排序：根输入顺序不同但依赖闭包
相同的请求得到完全相同的输出，路由层据此对规范化 JSON 计算 SHA-256
``bundle_digest``。

错误与 :mod:`.resolve` 一致：文档 / 修订 / 指针不存在 → ``ResolutionError``
（对应 422），携带从根文档出发的完整引用链。

算法本身不依赖 FastAPI / SQLite，可独立单测。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .merge import json_pointer
from .resolve import (
    ResolutionError,
    evaluate_pointer,
    match_reference,
    parse_pointer,
)


def _chain_entry(
    document_id: str,
    revision: Optional[int],
    pointer: str,
    ref: Optional[str],
    at: Optional[str],
) -> dict[str, Any]:
    """引用链中的一跳，与 :func:`.resolve.resolve_document` 的链路条目同构。"""
    return {
        "document_id": document_id,
        "revision": revision,
        "pointer": pointer,
        "ref": ref,
        "at": at,
    }


def lock_bundle(
    roots: list[tuple[str, Any]],
    get_current_revision: Callable[[str], Optional[int]],
    get_content: Callable[[str, int], Optional[Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """收集 *roots* 的依赖闭包，返回 ``(locked_roots, snapshots, edges)``。

    :param roots: ``(文档 ID, "current" 或正整数修订号)`` 列表，按输入顺序处理；
    :param get_current_revision: 读取文档当前修订号，文档不存在时返回 None；
    :param get_content: 读取指定修订的内容（已解析为 JSON 值），
                        文档或修订不存在时返回 None；
    :returns:
        * ``locked_roots`` —— 改写后的根列表：``current`` 已固定为数字修订，
          按 (文档, 修订) 去重并排序，每项 ``{"document_id", "revision"}``；
        * ``snapshots`` —— 闭包内全部 (文档, 修订) 的原始内容，
          按 (文档, 修订) 排序，每项 ``{"document_id", "revision", "content"}``；
        * ``edges`` —— 全部依赖边，按 (from 文档, from 修订, from 指针,
          to 文档, to 修订, to 指针) 排序，每项::

              {"from": {"document_id", "revision", "at"},
               "to": {"document_id", "revision", "pointer"},
               "ref": <原始引用串>}

          其中 ``from.at`` 是引用在源快照中的位置（RFC 6901），
          ``to.pointer`` 是引用目标指针；
    :raises ResolutionError: 文档 / 修订 / 指针不存在（422），携带完整引用链。
    """
    current_cache: dict[str, Optional[int]] = {}
    content_cache: dict[tuple[str, int], Any] = {}

    def pinned_current(doc_id: str) -> Optional[int]:
        """文档当前修订号；同一快照内多次读取结果一致，缓存避免重复查询。"""
        if doc_id not in current_cache:
            current_cache[doc_id] = get_current_revision(doc_id)
        return current_cache[doc_id]

    def content_of(doc_id: str, revision: int) -> Any:
        """指定修订的内容；同一快照内结果一致，缓存避免重复查询（含缺失的 None）。"""
        key = (doc_id, revision)
        if key not in content_cache:
            content_cache[key] = get_content(doc_id, revision)
        return content_cache[key]

    # ---- ① 固定根修订：current → 当次快照所见的数字修订；按 (文档, 修订) 去重 ----
    pinned_roots: list[tuple[str, int]] = []
    root_seen: set[tuple[str, int]] = set()
    for doc_id, spec in roots:
        current = pinned_current(doc_id)
        if current is None:
            raise ResolutionError(
                422,
                "document_not_found",
                [
                    _chain_entry(
                        doc_id, spec if isinstance(spec, int) else None, "", None, None
                    )
                ],
                document_id=doc_id,
                reason="根文档不存在",
            )
        revision = current if spec == "current" else spec
        if content_of(doc_id, revision) is None:
            raise ResolutionError(
                422,
                "revision_not_found",
                [_chain_entry(doc_id, revision, "", None, None)],
                document_id=doc_id,
                revision=revision,
                current_revision=current,
                reason="根文档的指定修订不存在",
            )
        if (doc_id, revision) not in root_seen:
            root_seen.add((doc_id, revision))
            pinned_roots.append((doc_id, revision))

    snapshots: dict[tuple[str, int], Any] = {}
    edges: list[dict[str, Any]] = []

    def follow(
        match: Any,
        ref_text: str,
        src_id: str,
        src_rev: int,
        path: list[str],
        stack: list[dict[str, Any]],
    ) -> None:
        """校验并记录一条依赖边，再把目标 (文档, 修订) 纳入闭包递归遍历。"""
        target_id, revision_token, pointer = (
            match.group(1),
            match.group(2),
            match.group(3),
        )
        at = json_pointer(path)

        # ① 固定目标修订：@current → 当次快照所见的数字修订；文档不存在 → 422
        target_current = pinned_current(target_id)
        if target_current is None:
            hop = _chain_entry(
                target_id,
                int(revision_token) if revision_token != "current" else None,
                pointer,
                ref_text,
                at,
            )
            raise ResolutionError(
                422,
                "document_not_found",
                stack + [hop],
                document_id=target_id,
                reason="被引用文档不存在",
            )
        target_revision = (
            target_current if revision_token == "current" else int(revision_token)
        )
        hop = _chain_entry(target_id, target_revision, pointer, ref_text, at)

        # ② 指针语法（转义非法视为指针不可解析）
        segments = parse_pointer(pointer)
        if segments is None:
            raise ResolutionError(
                422,
                "pointer_not_found",
                stack + [hop],
                document_id=target_id,
                revision=target_revision,
                pointer=pointer,
                reason="RFC 6901 指针转义非法",
            )

        # ③ 指定修订不存在 → revision_not_found
        content = content_of(target_id, target_revision)
        if content is None:
            raise ResolutionError(
                422,
                "revision_not_found",
                stack + [hop],
                document_id=target_id,
                revision=target_revision,
                current_revision=target_current,
                reason="被引用修订不存在",
            )

        # ④ 指针求值：路径在目标快照中不存在 → pointer_not_found
        try:
            evaluate_pointer(content, segments)
        except LookupError:
            raise ResolutionError(
                422,
                "pointer_not_found",
                stack + [hop],
                document_id=target_id,
                revision=target_revision,
                pointer=pointer,
                reason="指针在目标修订中不存在",
            ) from None

        # ⑤ 记录依赖边：@current 已固定为数字修订，ref 保留原始引用串；
        # 同一快照同一位置至多一条边（每个节点只遍历一次，位置唯一）
        edges.append(
            {
                "from": {"document_id": src_id, "revision": src_rev, "at": at},
                "to": {
                    "document_id": target_id,
                    "revision": target_revision,
                    "pointer": pointer,
                },
                "ref": ref_text,
            }
        )

        # ⑥ 递归收集：目标 (文档, 修订) 进入闭包（已入闭包时由 visit 去重）
        visit(target_id, target_revision, stack + [hop])

    def walk(
        value: Any,
        src_id: str,
        src_rev: int,
        path: list[str],
        stack: list[dict[str, Any]],
    ) -> None:
        """遍历整篇快照内容，符合引用语法的 $ref 各产生一条依赖边。"""
        if isinstance(value, dict):
            match = match_reference(value)
            if match is not None:
                follow(match, value["$ref"], src_id, src_rev, path, stack)
                return
            for key, item in value.items():
                walk(item, src_id, src_rev, path + [key], stack)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, src_id, src_rev, path + [str(index)], stack)

    def visit(doc_id: str, revision: int, stack: list[dict[str, Any]]) -> None:
        """把 (文档, 修订) 纳入闭包并遍历其整篇快照；按 (文档, 修订) 去重。"""
        if (doc_id, revision) in snapshots:
            return
        # 调用方已校验存在性（根在步骤①、目标在 follow 内），同一读事务内必然存在
        content = content_of(doc_id, revision)
        snapshots[(doc_id, revision)] = content
        walk(content, doc_id, revision, [], stack)

    # ---- ② 从各根出发递归收集闭包（深度优先，按根输入顺序） ----
    for doc_id, revision in pinned_roots:
        visit(doc_id, revision, [_chain_entry(doc_id, revision, "", None, None)])

    # ---- ③ 全部输出按 (文档, 修订, 指针) 稳定排序：闭包相同则输出完全相同 ----
    locked_roots = [
        {"document_id": doc_id, "revision": revision}
        for doc_id, revision in sorted(pinned_roots)
    ]
    snapshot_list = [
        {
            "document_id": doc_id,
            "revision": revision,
            "content": snapshots[(doc_id, revision)],
        }
        for doc_id, revision in sorted(snapshots)
    ]
    edge_list = sorted(
        edges,
        key=lambda edge: (
            edge["from"]["document_id"],
            edge["from"]["revision"],
            edge["from"]["at"],
            edge["to"]["document_id"],
            edge["to"]["revision"],
            edge["to"]["pointer"],
        ),
    )
    return locked_roots, snapshot_list, edge_list
