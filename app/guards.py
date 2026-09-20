"""更新前置条件（guards）：写事务内按 RFC 6901 指针检查当前内容（无框架依赖，可单测）。

每条 guard 恰好声明两种条件之一：

* ``expected`` —— 当前内容在指针处的值必须与之相等（按规范化 JSON 比较：
  键序无关；``1`` / ``1.0`` / ``true`` 这类 Python ``==`` 会判等、
  但落库字节不同的值视为不相等，与差异审计的口径一致）；
* ``missing: true`` —— 指针处必须**不存在**（真实存储的 JSON ``null``
  是存在的值，不算缺失）。

指针语法（``""`` 或 ``/`` 开头、``~0``/``~1`` 转义）复用引用解析的
:func:`resolve.parse_pointer`；语法合法性已在请求模型层校验，
本模块对非法指针抛 ``ValueError``（防御性分支，正常请求不可达）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Protocol

from .resolve import evaluate_pointer, parse_pointer


class GuardLike(Protocol):
    """:func:`check_guards` 所需的最小 guard 接口（由 ``schemas.Guard`` 实现）。"""

    path: str
    expected: Any
    missing: bool


def _canonical(value: Any) -> str:
    """确定性序列化（与 db.canonical 同参数），用于整体值相等判断。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def check_guards(content: Any, guards: Iterable[GuardLike]) -> list[dict[str, Any]]:
    """按输入顺序对 *content*（当前修订）评估全部 guard，返回未通过项列表。

    空列表表示全部通过。每个未通过项形如
    ``{"path", "expected", "actual", "missing"}``：

    * ``expected`` / ``actual`` —— 期望值 / 当前实际值，缺失一侧为 ``None``；
    * ``missing`` —— 缺失侧标注：``["actual"]`` 表示期望等值但指针处不存在，
      ``["expected"]`` 表示期望缺失但指针处存在（与三方冲突、差异审计的
      ``missing`` 约定一致，据此区分“键不存在”与真实存储的 ``null``）。
    """
    failures: list[dict[str, Any]] = []
    for guard in guards:
        segments = parse_pointer(guard.path)
        if segments is None:  # 模型层已拒绝非法指针；防御性分支
            raise ValueError(f"非法 RFC 6901 指针: {guard.path!r}")
        try:
            actual = evaluate_pointer(content, segments)
            actual_missing = False
        except LookupError:
            actual = None
            actual_missing = True

        if guard.missing:
            # 期望缺失：指针处存在即失败（真实 null 也算存在）
            if not actual_missing:
                failures.append(
                    {
                        "path": guard.path,
                        "expected": None,
                        "actual": actual,
                        "missing": ["expected"],
                    }
                )
        elif actual_missing:
            # 期望等值但指针处不存在
            failures.append(
                {
                    "path": guard.path,
                    "expected": guard.expected,
                    "actual": None,
                    "missing": ["actual"],
                }
            )
        elif _canonical(actual) != _canonical(guard.expected):
            # 期望等值但两侧不同
            failures.append(
                {
                    "path": guard.path,
                    "expected": guard.expected,
                    "actual": actual,
                    "missing": [],
                }
            )
    return failures
