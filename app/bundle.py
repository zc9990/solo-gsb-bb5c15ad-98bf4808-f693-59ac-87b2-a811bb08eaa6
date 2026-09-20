"""跨文档依赖锁定包：把若干根文档的 ``$ref`` 依赖闭包固定为数字修订（无框架依赖，可单测）。

与 :mod:`app.resolve` 的区别
---------------------------
``resolved`` 接口把引用**展开**成单一文档视图（依赖成环是错误）；本模块不展开
任何引用，而是遍历各**固定快照**内符合现有语法的 ``$ref``（仅含 ``$ref`` 键、
值形如 ``doc:<ID>@<正整数|current>#<RFC6901指针>`` 的对象），产出一份
"锁定包"：

* ``@current`` 固定为当次快照所见的数字修订，``@<正整数>`` 数字引用保持不变；
* 递归收集被引用的（文档, 修订）快照，按 **文档+修订** 去重；
* 依赖边记录「源文档/修订/指针位置 → 目标文档/修订/指针」及改写后的引用串；
* 根列表、快照、边全部按文档、修订、指针稳定排序，对规范化 JSON 计算
  SHA-256 ``bundle_digest``——根输入顺序不同但依赖闭包相同的请求得到相同摘要。

依赖成环在这里**不是错误**：环上每个 (文档, 修订, 指针) 目标只遍历一次，
边照常记录（锁定只需要完整闭包，不需要无环展开）。

错误
----
文档 / 修订 / 指针不存在（指针转义非法同样视为指针不可解析）→
``ResolutionError``（对应 422），携带从某个根文档出发的完整引用链。
全程只读：调用方在同一 SQLite 读事务快照内提供两个读取回调，失败不产生任何写入。
"""
from __future__ import annotations

import hashlib
from collections import deque
from typing import Any, Callable, Optional

from .db import canonical
from .merge import json_pointer
from .resolve import (
    ResolutionError,
    evaluate_pointer,
    match_reference,
    parse_pointer,
)

#: 快照身份：(文档 ID, 修订号, 指针令牌序列)；根文档与整篇引用的指针为 ()
Identity = tuple[str, int, tuple[str, ...]]


def _chain_entry(
    document_id: str,
    revision: Optional[int],
    pointer: str,
    ref: Optional[str],
    at: Optional[str],
) -> dict[str, Any]:
    """与 resolve 接口一致的引用链表项形态。"""
    return {
        "document_id": document_id,
        "revision": revision,
        "pointer": pointer,
        "ref": ref,
        "at": at,
    }


def _iter_refs(value: Any, rel_path: list[str]) -> Any:
    """在子树内前序遍历，产出 ``(match, ref_text, rel_path)``。

    与 resolve 的遍历语义一致：命中引用形态的对象即作为引用产出且**不再下钻**
    （其值恰好只有一个 ``$ref`` 键）；其余对象递归键值、数组递归元素；
    非引用形态的 ``$ref`` 对象按普通对象继续递归。
    """
    if isinstance(value, dict):
        match = match_reference(value)
        if match is not None:
            yield match, value["$ref"], rel_path
            return
        for key, item in value.items():
            yield from _iter_refs(item, rel_path + [key])
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_refs(item, rel_path + [str(index)])


def lock_bundle(
    roots: list[tuple[str, Any]],
    get_current_revision: Callable[[str], Optional[int]],
    get_content: Callable[[str, int], Optional[Any]],
) -> dict[str, Any]:
    """计算 *roots* 的跨文档依赖锁定包。

    :param roots: ``[(document_id, "current" | 正整数修订号), ...]``，1～20 项
                  （数量由请求模型约束）；重复根按 (文档, 固定修订) 去重；
    :param get_current_revision: 读取文档当前修订号，文档不存在时返回 None；
    :param get_content: 读取指定修订的内容（已解析为 JSON 值），
                        文档或修订不存在时返回 None；
    :returns: ``{"roots", "documents", "edges", "bundle_digest"}``；
    :raises ResolutionError: 文档 / 修订 / 指针不存在（422，含完整引用链）。
    """
    # 已收集的原始快照：(文档, 修订) → 内容；按文档+修订去重
    snapshots: dict[tuple[str, int], Any] = {}
    edges: list[dict[str, Any]] = []
    edge_seen: set[tuple[str, int, str]] = set()
    # 已开始遍历的目标（含根）；环上再次出现只补记边、不重复遍历
    visited: set[Identity] = set()
    root_identities: set[Identity] = set()
    # 非根目标的入边（首次发现者优先）：目标身份 → (来源身份, 链表项)
    incoming: dict[Identity, tuple[Identity, dict[str, Any]]] = {}

    def build_chain(failing_identity: Identity, failing_hop: dict[str, Any]):
        """沿 incoming 回溯到某个根，拼成 根 → … → 失败跳 的完整引用链。"""
        chain: list[dict[str, Any]] = []
        current = failing_identity
        while current not in root_identities:
            parent_identity, hop = incoming[current]
            chain.append(hop)
            current = parent_identity
        chain.append(_chain_entry(current[0], current[1], "", None, None))
        chain.reverse()
        chain.append(failing_hop)
        return chain

    def fail(
        src_identity: Identity,
        code: str,
        hop: dict[str, Any],
        reason: str,
        **extra: Any,
    ) -> ResolutionError:
        return ResolutionError(
            422, code, build_chain(src_identity, hop), reason=reason, **extra
        )

    # ---- 固定根修订并播种（按文档、修订排序，保证失败点与输出稳定） ----
    pinned_roots: list[tuple[str, int]] = []
    queue: deque[tuple[str, int, tuple[str, ...], Identity]] = deque()
    for doc_id, spec in sorted(roots, key=lambda item: (item[0], str(item[1]))):
        current = get_current_revision(doc_id)
        if current is None:
            raise ResolutionError(
                422,
                "document_not_found",
                [
                    _chain_entry(
                        doc_id,
                        spec if isinstance(spec, int) and not isinstance(spec, bool)
                        else None,
                        "",
                        None,
                        None,
                    )
                ],
                document_id=doc_id,
                reason="根文档不存在",
            )
        revision = current if spec == "current" else spec
        content = get_content(doc_id, revision)
        if content is None:
            raise ResolutionError(
                422,
                "revision_not_found",
                [_chain_entry(doc_id, revision, "", None, None)],
                document_id=doc_id,
                revision=revision,
                current_revision=current,
                reason="根文档的指定修订不存在",
            )
        snapshots.setdefault((doc_id, revision), content)
        if (doc_id, revision) not in pinned_roots:
            pinned_roots.append((doc_id, revision))
        identity = (doc_id, revision, ())
        if identity not in root_identities:
            root_identities.add(identity)
            visited.add(identity)
            queue.append((doc_id, revision, (), identity))

    # ---- 在固定快照闭包内逐个子树遍历 $ref ----
    while queue:
        src_id, src_rev, base_segments, src_identity = queue.popleft()
        source = snapshots[(src_id, src_rev)]
        # 子树在入队前已验证指针可解析
        subtree = evaluate_pointer(source, list(base_segments))

        for match, ref_text, rel_segments in _iter_refs(subtree, []):
            target_id, token, pointer = (
                match.group(1),
                match.group(2),
                match.group(3),
            )
            # 引用位置：源文档内的完整 RFC 6901 指针
            at = json_pointer(list(base_segments) + rel_segments)

            # ① 固定目标修订：文档不存在 → document_not_found
            target_current = get_current_revision(target_id)
            if target_current is None:
                hop = _chain_entry(
                    target_id,
                    int(token) if token != "current" else None,
                    pointer,
                    ref_text,
                    at,
                )
                raise fail(
                    src_identity,
                    "document_not_found",
                    hop,
                    "被引用文档不存在",
                    document_id=target_id,
                )
            target_revision = (
                target_current if token == "current" else int(token)
            )

            # ② 指针语法（转义非法视为指针不可解析）
            segments = parse_pointer(pointer)
            if segments is None:
                hop = _chain_entry(
                    target_id, target_revision, pointer, ref_text, at
                )
                raise fail(
                    src_identity,
                    "pointer_not_found",
                    hop,
                    "RFC 6901 指针转义非法",
                    document_id=target_id,
                    revision=target_revision,
                    pointer=pointer,
                )
            target_identity: Identity = (
                target_id,
                target_revision,
                tuple(segments),
            )

            # ③ 指定修订快照不存在 → revision_not_found
            target_content = snapshots.get((target_id, target_revision))
            if target_content is None:
                target_content = get_content(target_id, target_revision)
                if target_content is None:
                    hop = _chain_entry(
                        target_id, target_revision, pointer, ref_text, at
                    )
                    raise fail(
                        src_identity,
                        "revision_not_found",
                        hop,
                        "被引用修订不存在",
                        document_id=target_id,
                        revision=target_revision,
                        current_revision=target_current,
                        pointer=pointer,
                    )
                snapshots.setdefault((target_id, target_revision), target_content)

            # ④ 指针求值：路径在目标快照中不存在 → pointer_not_found
            try:
                evaluate_pointer(target_content, segments)
            except LookupError:
                hop = _chain_entry(
                    target_id, target_revision, pointer, ref_text, at
                )
                raise fail(
                    src_identity,
                    "pointer_not_found",
                    hop,
                    "指针在目标修订中不存在",
                    document_id=target_id,
                    revision=target_revision,
                    pointer=pointer,
                )

            # 记录依赖边：同一源位置不重复记录
            edge_key = (src_id, src_rev, at)
            if edge_key not in edge_seen:
                edge_seen.add(edge_key)
                edges.append(
                    {
                        "document_id": src_id,
                        "revision": src_rev,
                        "at": at,
                        "ref": ref_text,
                        "target_document_id": target_id,
                        "target_revision": target_revision,
                        "pointer": pointer,
                        "locked_ref": f"doc:{target_id}@{target_revision}#{pointer}",
                    }
                )

            # 环上目标只遍历一次（边已在上面记录）；不同分支指向同一目标同理
            if target_identity not in visited:
                visited.add(target_identity)
                incoming.setdefault(
                    target_identity,
                    (
                        src_identity,
                        _chain_entry(
                            target_id, target_revision, pointer, ref_text, at
                        ),
                    ),
                )
                queue.append(
                    (target_id, target_revision, tuple(segments), target_identity)
                )

    # ---- 稳定排序输出 ----
    # 改写后的根列表只含入参根（current 已固定为数字修订）；传递依赖在 documents/edges
    out_roots = [
        {"document_id": doc_id, "revision": revision}
        for doc_id, revision in sorted(set(pinned_roots))
    ]
    documents = [
        {"document_id": doc_id, "revision": revision, "content": content}
        for (doc_id, revision), content in sorted(
            snapshots.items(), key=lambda item: item[0]
        )
    ]
    out_edges = sorted(edges, key=lambda edge: (edge["document_id"], edge["revision"], edge["at"]))

    # 摘要对「根 + 全部快照 + 全部边」的规范化 JSON 计算；
    # 根顺序、遍历顺序都已归一化，故仅依赖闭包内容。
    payload: dict[str, Any] = {
        "roots": out_roots,
        "documents": documents,
        "edges": out_edges,
    }
    digest = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
    return {**payload, "bundle_digest": digest}
