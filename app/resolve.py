"""跨文档引用解析：``$ref`` 引用的递归展开（无框架依赖，可单测）。

引用形式
--------
对象**恰好**只含一个 ``$ref`` 键、且值整体匹配
``doc:<ID>@<正整数|current>#<RFC6901指针>`` 时视为引用：

* ``@current`` —— 解析时（同一 SQLite 读事务快照内）固定为目标文档的当前修订；
* ``@<正整数>`` —— 固定为指定历史修订；
* ``#`` 之后为 RFC 6901 JSON Pointer（单独的 ``#`` 指整篇文档），
  数组下标为 ``0`` 或不带前导零的正整数；指针按字面解释，不做百分号解码。

不满足上述形态的 ``$ref`` 对象只是普通数据，原样保留。

解析语义
--------
* 递归替换对象与数组中的引用；被引用的目标值本身也会继续解析
  （目标文档里的嵌套引用在目标上下文中展开）；
* 同一 (文档, 修订, 指针) 目标在**当前解析路径**上再次出现 → 依赖成环；
  不同分支重复引用同一目标是合法的（菱形），直接复用首次解析结果；
* 最大解析深度 :data:`MAX_DEPTH`（沿解析路径的引用跳数上限）。

错误
----
* 文档 / 修订 / 指针不存在、超过最大深度
  → ``ResolutionError``（对应 422），携带从根文档出发的完整引用链；
* 依赖成环 → ``ResolutionError``（对应 409），携带首尾闭合的环链。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from .merge import json_pointer

#: 引用值整体格式：doc:<ID>@<正整数|current>#<RFC6901指针>
REF_RE = re.compile(r"^doc:([A-Za-z0-9][A-Za-z0-9._~-]*)@([1-9][0-9]*|current)#(.*)$")

#: RFC 6901 数组下标：0 或不带前导零的正整数
_INDEX_RE = re.compile(r"^(0|[1-9][0-9]*)$")

#: 非法的 ~ 转义（~ 后只允许跟 0 或 1）
_BAD_ESCAPE_RE = re.compile(r"~(?![01])")

#: 最大解析深度：沿解析路径最多跟随的引用跳数
MAX_DEPTH = 32


class ResolutionError(Exception):
    """引用解析失败；携带 HTTP 状态码、错误码与引用链，由路由翻译为响应体。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        chain: list[dict[str, Any]],
        **extra: Any,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.chain = chain
        self.extra = extra
        super().__init__(code)


def match_reference(value: Any) -> Optional[re.Match]:
    """*value* 为「仅含 $ref 且值形如 doc:...」的对象时返回正则匹配，否则 None。"""
    if isinstance(value, dict) and len(value) == 1:
        ref = value.get("$ref")
        if isinstance(ref, str):
            return REF_RE.match(ref)
    return None


def parse_pointer(pointer: str) -> Optional[list[str]]:
    """解析 RFC 6901 JSON Pointer 为引用令牌序列；格式非法时返回 None。

    空指针 ``""`` 指整篇文档；``~1`` → ``/``、``~0`` → ``~``，
    其余 ``~`` 用法非法。
    """
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        return None
    segments: list[str] = []
    for raw in pointer.split("/")[1:]:
        if _BAD_ESCAPE_RE.search(raw):
            return None
        segments.append(raw.replace("~1", "/").replace("~0", "~"))
    return segments


def evaluate_pointer(content: Any, segments: list[str]) -> Any:
    """在 *content* 上按引用令牌求值；任一段无法定位时抛 ``LookupError``。"""
    current = content
    for segment in segments:
        if isinstance(current, dict):
            if segment not in current:
                raise LookupError(segment)
            current = current[segment]
        elif isinstance(current, list):
            if _INDEX_RE.fullmatch(segment) is None:
                raise LookupError(segment)
            index = int(segment)
            if index >= len(current):
                raise LookupError(segment)
            current = current[index]
        else:
            raise LookupError(segment)
    return current


def resolve_document(
    root_id: str,
    revision_spec: Any,
    get_current_revision: Callable[[str], Optional[int]],
    get_content: Callable[[str, int], Optional[Any]],
) -> tuple[Any, int, list[dict[str, Any]]]:
    """递归展开 *root_id* 在 *revision_spec* 修订下的全部 ``$ref`` 引用。

    :param revision_spec: ``"current"`` 或正整数修订号；
    :param get_current_revision: 读取文档当前修订号，文档不存在时返回 None；
    :param get_content: 读取指定修订的内容（已解析为 JSON 值），
                        文档或修订不存在时返回 None；
    :returns: ``(resolved, root_revision, dependencies)``。
              dependencies 按首次遍历排序、按 (文档, 修订, 指针) 去重，
              每项为 ``{"document_id", "revision", "pointer"}``；
    :raises ResolutionError: 文档 / 修订 / 指针不存在（422）、
                             依赖成环（409）、超过最大深度（422）。
    """
    dependencies: list[dict[str, Any]] = []
    dep_seen: set[tuple[str, int, tuple[str, ...]]] = set()
    resolved_cache: dict[tuple[str, int, tuple[str, ...]], Any] = {}
    current_cache: dict[str, Optional[int]] = {}

    def pinned_current(doc_id: str) -> Optional[int]:
        """文档当前修订号；同一快照内多次读取结果一致，缓存避免重复查询。"""
        if doc_id not in current_cache:
            current_cache[doc_id] = get_current_revision(doc_id)
        return current_cache[doc_id]

    def chain_entry(
        document_id: str,
        revision: Optional[int],
        pointer: str,
        ref: Optional[str],
        at: Optional[str],
    ) -> dict[str, Any]:
        return {
            "document_id": document_id,
            "revision": revision,
            "pointer": pointer,
            "ref": ref,
            "at": at,
        }

    # ---- 固定根文档修订（与所有 current 引用同一读事务快照） ----
    root_current = pinned_current(root_id)
    if root_current is None:
        raise ResolutionError(
            422,
            "document_not_found",
            [
                chain_entry(
                    root_id,
                    revision_spec if isinstance(revision_spec, int) else None,
                    "",
                    None,
                    None,
                )
            ],
            document_id=root_id,
            reason="根文档不存在",
        )
    root_revision = root_current if revision_spec == "current" else revision_spec
    root_content = get_content(root_id, root_revision)
    if root_content is None:
        raise ResolutionError(
            422,
            "revision_not_found",
            [chain_entry(root_id, root_revision, "", None, None)],
            document_id=root_id,
            revision=root_revision,
            current_revision=root_current,
            reason="根文档的指定修订不存在",
        )

    # 解析路径栈：栈顶是正在展开的文档目标，用于环检测与引用链渲染
    stack: list[dict[str, Any]] = [chain_entry(root_id, root_revision, "", None, None)]
    identities: list[tuple[str, int, tuple[str, ...]]] = [(root_id, root_revision, ())]

    def resolve_ref(match: re.Match, ref_text: str, path: list[str], depth: int) -> Any:
        target_id, revision_token, pointer = (
            match.group(1),
            match.group(2),
            match.group(3),
        )
        at = json_pointer(path)

        # ① 固定目标修订：文档不存在 → document_not_found
        target_current = pinned_current(target_id)
        if target_current is None:
            hop = chain_entry(
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
        hop = chain_entry(target_id, target_revision, pointer, ref_text, at)

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
        identity = (target_id, target_revision, tuple(segments))

        # ③ 深度上限：沿解析路径第 MAX_DEPTH+1 跳被拒绝
        if depth + 1 > MAX_DEPTH:
            raise ResolutionError(
                422,
                "max_depth_exceeded",
                stack + [hop],
                document_id=target_id,
                revision=target_revision,
                pointer=pointer,
                max_depth=MAX_DEPTH,
                reason="超过最大解析深度",
            )

        # ④ 环检测：同一目标在当前解析路径上再次出现 → 409，链首尾闭合
        for index, known in enumerate(identities):
            if known == identity:
                raise ResolutionError(
                    409,
                    "reference_cycle",
                    stack[index:] + [hop],
                    document_id=target_id,
                    revision=target_revision,
                    pointer=pointer,
                    reason="依赖成环",
                )

        # ⑤ 菱形复用：同一目标已在其他分支完整解析过，直接复用结果
        if identity in resolved_cache:
            return resolved_cache[identity]

        # ⑥ 指定修订不存在 → revision_not_found
        content = get_content(target_id, target_revision)
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

        # ⑦ 指针求值：路径在目标快照中不存在 → pointer_not_found
        try:
            target = evaluate_pointer(content, segments)
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

        # 记录依赖：按首次遍历顺序、按 (文档, 修订, 指针) 去重
        if identity not in dep_seen:
            dep_seen.add(identity)
            dependencies.append(
                {
                    "document_id": target_id,
                    "revision": target_revision,
                    "pointer": pointer,
                }
            )

        # 递归解析目标值（目标文档里的嵌套引用在目标上下文中展开）
        stack.append(hop)
        identities.append(identity)
        try:
            resolved = resolve_value(target, [], depth + 1)
        finally:
            stack.pop()
            identities.pop()
        resolved_cache[identity] = resolved
        return resolved

    def resolve_value(value: Any, path: list[str], depth: int) -> Any:
        if isinstance(value, dict):
            match = match_reference(value)
            if match is not None:
                return resolve_ref(match, value["$ref"], path, depth)
            return {
                key: resolve_value(item, path + [key], depth)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                resolve_value(item, path + [str(index)], depth)
                for index, item in enumerate(value)
            ]
        return value

    resolved_root = resolve_value(root_content, [], 0)
    return resolved_root, root_revision, dependencies
