"""JSON Merge Patch (RFC 7386) 与三方合并核心算法。

规则概述
--------
* ``apply_merge_patch`` 实现 RFC 7386：
  - patch 为对象时按键合并，键值为 ``null`` 表示删除该键；
  - patch 为其它任何类型（数组、字符串、数字、布尔、null）时，整体替换目标。
* ``three_way_merge`` 以「基线 base、服务端当前 current、来稿 theirs」递归比较：
  - 三处皆为对象时按键递归；
  - 否则按标量比较：current == base（当前版未改）取 theirs；
    theirs == base（来稿未改）取 current；双方都改且相同取相同值；
    双方都改且不同 → 冲突；
  - 删除用内部哨兵 ``MISSING`` 表示（JSON 没有“键缺失”值，
    因为合法的 ``null`` 是一种可存储的真实值）。

算法本身不依赖 FastAPI / SQLite，可独立单测。
"""
from __future__ import annotations

from typing import Any, Tuple

# 键缺失（被删除）的内部哨兵。对象身份比较，外部 JSON 不可能产生这个值。
MISSING: Any = object()


def apply_merge_patch(target: Any, patch: Any) -> Any:
    """按 RFC 7386 把 *patch* 应用到 *target*，返回结果。

    纯函数，不修改入参（浅拷贝被修改的 dict 层级）。
    """
    if isinstance(patch, dict):
        if not isinstance(target, dict):
            # RFC 7386 §2: target 不是对象时先以空对象替换，再逐键打补丁
            result: dict[str, Any] = {}
        else:
            result = dict(target)
        for key, value in patch.items():
            if value is None:
                # null 表示删除（键不存在时为 no-op）
                result.pop(key, None)
            else:
                result[key] = apply_merge_patch(result.get(key, MISSING), value)
        return result
    # 非对象 patch：整体替换（None 即整体替换为 JSON null）
    return patch


def json_pointer(key_path: list[str]) -> str:
    """把键序列渲染为 RFC 6901 JSON Pointer，根为 ``""``。"""
    if not key_path:
        return ""
    return "/" + "/".join(k.replace("~", "~0").replace("/", "~1") for k in key_path)


def three_way_merge(
    base: Any, current: Any, theirs: Any
) -> Tuple[Any, list[dict[str, Any]]]:
    """递归三方合并。

    :returns: ``(merged, conflicts)``。*conflicts* 为空时 *merged* 为最终文档；
              非空时 *merged* 不可用（调用方不得写入）。
    """
    conflicts: list[dict[str, Any]] = []

    def present(value: Any) -> bool:
        return value is not MISSING

    def node(b: Any, c: Any, t: Any, path: list[str]) -> Any:
        # 三处（或对其都存在的两处）都是对象 → 递归按键合并。
        # base 缺失但 current / theirs 都新建了对象时，也允许按子键递归，
        # 避免“双方各自新增嵌套对象”这种本可合并的情况被误判为冲突。
        c_obj = present(c) and isinstance(c, dict)
        t_obj = present(t) and isinstance(t, dict)
        b_obj = present(b) and isinstance(b, dict)
        if (b_obj and c_obj and t_obj) or (not present(b) and c_obj and t_obj):
            if not present(b):
                b_eff: Any = {}
            else:
                b_eff = b
            merged_obj: dict[str, Any] = {}
            for key in set(b_eff) | set(c) | set(t):
                bv = b_eff.get(key, MISSING)
                cv = c.get(key, MISSING)
                tv = t.get(key, MISSING)
                mv = node(bv, cv, tv, path + [key])
                if mv is not MISSING:
                    merged_obj[key] = mv
            return merged_obj

        # 标量（含数组——数组在 Merge Patch 中整体替换，不按索引合并）
        if c == b:
            # 服务端当前版与基线一致：这条路径只有来稿动过，直接用来稿
            return t
        if t == b:
            # 来稿与基线一致：只有服务端动过，保留当前版
            return c
        if c == t:
            # 双方都改了，但改成了同一个结果
            return c
        # 双方都改且不一致 → 冲突（MISSING 哨兵保留，序列化时由 present_conflict 处理）
        conflicts.append(
            {
                "path": json_pointer(path),
                "base": b,
                "current": c,
                "theirs": t,
            }
        )
        return MISSING

    merged = node(base, current, theirs, [])
    return merged, conflicts


def merge_patch_diff(old: Any, new: Any) -> Any:
    """构造把 *old* 变成 *new* 的 RFC 7386 Merge Patch（最小对象级差分）。

    与 :func:`apply_merge_patch` 互逆：``apply_merge_patch(old, merge_patch_diff(old, new))``
    恒等于 *new*。用于撤销接口——对目标修订的「前快照 → 后快照」取逆，
    得到的补丁应用到任何与前快照同构的基线上，都表达同一次逆向变更。

    * 两侧都是对象时按键递归：新增/整体变更的值直接给出，删除的键给 ``null``，
      子对象收敛为空时省略（避免 ``{}`` 这种无意义层级）；
    * 任一侧不是对象（数组 / 标量 / null）→ 整体替换，补丁直接为 *new*
      （数组不按下标差分，与 RFC 7386 的整体替换语义一致）；
    * 两侧相等 → 空对象补丁（no-op）。

    纯函数，不修改入参。
    """
    if isinstance(old, dict) and isinstance(new, dict):
        patch: dict[str, Any] = {}
        for key in set(old) | set(new):
            if key not in new:
                # 键被删除：null 是 Merge Patch 的删除标记
                patch[key] = None
            elif key not in old:
                # 新增键：直接给值
                patch[key] = new[key]
            elif old[key] != new[key]:
                sub = merge_patch_diff(old[key], new[key])
                # 两侧皆为对象但子级无差异时不会进入此分支；
                # 跳过收敛为空的嵌套补丁（仅当旧值恰为 {} 等边界情形）。
                if sub != {}:
                    patch[key] = sub
        return patch
    # 至少一侧不是对象（或两侧对象身份不同但相等的情况已被 != 排除）：整体替换
    return new


def present_conflict(conflict: dict[str, Any]) -> dict[str, Any]:
    """把冲突记录转换成对外响应（MISSING → null + missing 标注）。"""
    missing = [
        side
        for side in ("base", "current", "theirs")
        if conflict[side] is MISSING
    ]
    return {
        "path": conflict["path"],
        "values": {side: (None if conflict[side] is MISSING else conflict[side])
                   for side in ("base", "current", "theirs")},
        "missing": missing,
    }
