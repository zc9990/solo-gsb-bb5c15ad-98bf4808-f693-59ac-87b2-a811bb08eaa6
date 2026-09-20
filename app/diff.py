"""两个已存在修订快照之间的递归差异（审计用）。

规则概述
--------
* 两侧皆为对象时按键递归；任一侧不是对象（数组 / 标量 / null）时作为
  **整体值** 比较 —— 数组不按下标差分，与 RFC 7386 的整体替换语义一致；
* 键只存在于旧快照 → ``remove``；只存在于新快照 → ``add``；
  两侧都在但值不同 → ``replace``；新增 / 删除的子树作为整体值挂在
  该路径上，不再向下展开；
* 每个变更项同时给出 ``before`` / ``after``，并用 ``missing`` 标注哪一侧
  键缺失，以区分“键不存在”与真实存储的 JSON ``null``
  （与三方合并冲突体的 ``missing`` 约定一致）；
* 结果按 RFC 6901 JSON Pointer 路径稳定排序；空差异返回空列表。

算法本身不依赖 FastAPI / SQLite，可独立单测。
"""
from __future__ import annotations

import json
from typing import Any

from .merge import json_pointer

#: 变更操作类型（响应中的计数键也按此顺序给出）
OPS = ("add", "remove", "replace")


def _canonical(value: Any) -> str:
    """确定性序列化（与 db.canonical 同参数），用于整体值相等判断。

    区分 ``true``/``1``、``1``/``1.0`` 这类 Python ``==`` 会判等、
    但落库字节不同的值，保证“两个快照有差异必报差异”。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _change(
    op: str, path: list[str], before: Any, after: Any, missing: list[str]
) -> dict[str, Any]:
    return {
        "op": op,
        "path": json_pointer(path),
        "before": before,
        "after": after,
        "missing": missing,
    }


def snapshot_diff(old: Any, new: Any) -> list[dict[str, Any]]:
    """递归比较 *old* 与 *new* 两个快照，返回按路径稳定排序的变更列表。

    每项形如 ``{"op", "path", "before", "after", "missing"}``：
    ``missing`` 为 ``["before"]``（add）、``["after"]``（remove）或
    ``[]``（replace，两侧都在）。纯函数，不修改入参。
    """
    changes: list[dict[str, Any]] = []

    def node(before: Any, after: Any, path: list[str]) -> None:
        if isinstance(before, dict) and isinstance(after, dict):
            for key in set(before) | set(after):
                child = path + [key]
                if key not in before:
                    changes.append(_change("add", child, None, after[key], ["before"]))
                elif key not in after:
                    changes.append(_change("remove", child, before[key], None, ["after"]))
                else:
                    node(before[key], after[key], child)
            return
        # 至少一侧不是对象（数组 / 标量 / null）：整体值比较
        if _canonical(before) != _canonical(after):
            changes.append(_change("replace", path, before, after, []))

    node(old, new, [])
    # 路径互不重复，按指针字符串排序即完全确定（Python sort 本身稳定）
    changes.sort(key=lambda change: change["path"])
    return changes
