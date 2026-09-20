"""历史字段归因（blame）：目标快照中每个叶子值最后由哪次修订改变。

规则概述
--------
* 叶子定义：对象按键递归到叶子；数组 / 标量 / ``null`` 整体视为叶子
  （数组不按下标展开，与 RFC 7386 的整体替换语义一致）；
  空对象没有可递归的子键，本身作为叶子（值 ``{}``）；
* 从修订 1 起**按修订顺序**比较相邻快照：叶子路径在两个快照间未变化
  → 沿用既有归属；新出现或值变化 → 归属于后一个快照的修订；
* 路径消失后其归属即被丢弃：之后再次出现时归属于**重建它**的修订，
  即使值与删除前完全相同；
* 值比较用确定性序列化（与 diff 模块同一约定），区分 ``1``/``1.0``/``true``
  这类 Python ``==`` 会判等、但落库字节不同的值；
* 结果按 RFC 6901 JSON Pointer 路径稳定排序。

算法本身不依赖 FastAPI / SQLite，可独立单测。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from .merge import json_pointer


@dataclass
class RevisionSnapshot:
    """一次修订的归因输入：修订号、（已解析的）快照内容与落库元数据。"""

    revision: int
    content: Any
    author: Optional[str]
    request_id: Optional[str]
    created_at: str


def _canonical(value: Any) -> str:
    """确定性序列化（与 db.canonical 同参数），用于叶子值相等判断。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _flatten(
    value: Any, path: tuple[str, ...], out: dict[tuple[str, ...], Any]
) -> None:
    """把快照压平为 叶子路径 → 叶子值。

    非空对象按键递归；数组 / 标量 / ``null`` / 空对象整体视为叶子。
    """
    if isinstance(value, dict) and value:
        for key, child in value.items():
            _flatten(child, path + (key,), out)
    else:
        out[path] = value


def compute_blame(snapshots: list[RevisionSnapshot]) -> list[dict[str, Any]]:
    """对**按修订顺序**排列的 *snapshots* 计算最后一个快照的叶子归因。

    返回目标快照每个叶子路径的归因项，按 RFC 6901 路径稳定排序；每项形如
    ``{"path", "value", "last_changed_revision", "author", "request_id",
    "created_at"}``，后四者取自该叶子最后归属的修订记录。
    纯函数，不修改入参；*snapshots* 为空时返回空列表。
    """
    # 叶子路径 -> (叶子值, 归属修订记录)；只保留当前快照仍存在的叶子，
    # 路径一旦消失，归属随之丢弃（再现时按“新出现”归属于重建修订）。
    attributed: dict[tuple[str, ...], tuple[Any, RevisionSnapshot]] = {}
    for snap in snapshots:
        leaves: dict[tuple[str, ...], Any] = {}
        _flatten(snap.content, (), leaves)
        current: dict[tuple[str, ...], tuple[Any, RevisionSnapshot]] = {}
        for path, value in leaves.items():
            previous = attributed.get(path)
            if previous is not None and _canonical(previous[0]) == _canonical(value):
                # 相邻两快照间叶子未变化：沿用既有归属
                current[path] = previous
            else:
                # 新出现（含删除后重建）或值变化：归属于本次修订
                current[path] = (value, snap)
        attributed = current

    entries = [
        {
            "path": json_pointer(list(path)),
            "value": value,
            "last_changed_revision": snap.revision,
            "author": snap.author,
            "request_id": snap.request_id,
            "created_at": snap.created_at,
        }
        for path, (value, snap) in attributed.items()
    ]
    # 路径互不重复，按指针字符串排序即完全确定（Python sort 本身稳定）
    entries.sort(key=lambda entry: entry["path"])
    return entries
