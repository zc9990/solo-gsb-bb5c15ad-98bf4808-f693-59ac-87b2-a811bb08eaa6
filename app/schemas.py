"""请求/响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .resolve import parse_pointer

# JSON 允许的值类型
_JSON_TYPES = (dict, list, str, int, float, bool, type(None))


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateDocument(StrictModel):
    id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        description="文档 ID；省略时服务端生成。允许字母数字及 . _ ~ -",
    )
    content: Any = Field(default_factory=dict, description="初始 JSON 配置")
    author: Optional[str] = Field(default=None, max_length=200)

    @field_validator("content")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("content 必须是合法的 JSON 值")
        return v


class Guard(StrictModel):
    """单条更新前置条件：恰好声明 ``expected``（等值）或 ``missing: true``（缺失）之一。"""

    path: str = Field(
        ...,
        max_length=1000,
        description="RFC 6901 JSON Pointer（空串指整篇文档），指向当前内容中被检查的位置",
    )
    expected: Any = Field(
        default=None,
        description="当前内容在 path 处必须等于的 JSON 值（与 missing 互斥）",
    )
    missing: bool = Field(
        default=False,
        description="为 true 时要求 path 处不存在（与 expected 互斥）",
    )

    @field_validator("path")
    @classmethod
    def _valid_pointer(cls, v: str) -> str:
        if parse_pointer(v) is None:
            raise ValueError("path 必须是合法的 RFC 6901 JSON Pointer")
        return v

    @field_validator("expected")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("expected 必须是合法的 JSON 值")
        return v

    @model_validator(mode="after")
    def _exactly_one_condition(self) -> "Guard":
        has_expected = "expected" in self.model_fields_set
        if has_expected and self.missing:
            raise ValueError("guard 不能同时声明 expected 与 missing: true 两种条件")
        if not has_expected and not self.missing:
            raise ValueError("guard 必须声明 expected 或 missing: true 之一")
        return self

    def fingerprint_form(self) -> dict[str, Any]:
        """幂等指纹用的规范化形态：path + 恰好一种条件。

        显式 ``missing: false`` 归一化为省略；显式 ``expected: null``
        （等值 null）与 ``missing: true``（期望缺失）是不同条件，形态可区分。
        """
        if "expected" in self.model_fields_set:
            return {"path": self.path, "expected": self.expected}
        return {"path": self.path, "missing": True}


class UpdateDocument(StrictModel):
    base_revision: int = Field(
        ..., ge=1, description="来稿所基于的修订号（文档读取时返回）"
    )
    request_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="调用方生成的去重 ID（如 UUID），同 ID+同内容幂等",
    )
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")
    guards: Optional[list[Guard]] = Field(
        default=None,
        description="可选前置条件：写事务内先按输入顺序检查当前修订，全部通过才合并",
    )
    author: Optional[str] = Field(default=None, max_length=200)
    lease_token: Optional[int] = Field(
        default=None,
        ge=1,
        description="写入租约 token：目标文档存在未过期租约时必须等于当前 fencing token；"
        "无有效租约时携带 token 同样被拒绝（旧 token 已失效）",
    )

    @field_validator("patch")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("patch 必须是合法的 JSON 值")
        return v


class RevertDocument(StrictModel):
    revision: int = Field(
        ..., ge=1, description="待撤销的目标修订号；必须存在且大于 1"
    )
    request_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="调用方生成的去重 ID（如 UUID），同 ID+同内容幂等",
    )
    author: Optional[str] = Field(default=None, max_length=200)


class ImpactPreview(StrictModel):
    """变更影响预演：不落库地预演一次 PATCH 对各根文档解析视图的影响。

    只读接口，不携带 ``request_id`` / ``author`` / ``guards`` / ``lease_token``：
    不创建修订、不占用幂等 ID、不改变租约。
    """

    base_revision: int = Field(
        ..., ge=1, description="来稿所基于的修订号（与 PATCH 语义相同）"
    )
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")
    root_document_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="需要预演解析影响的根文档 ID 列表（1～20 个；重复项按首次出现去重）",
    )

    @field_validator("patch")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("patch 必须是合法的 JSON 值")
        return v


class SetContract(StrictModel):
    # JSON Schema 根必须是对象或布尔（true/false 布尔 Schema）；
    # 进一步的合法性（关键字、$ref 等）由 validation.check_schema 做元校验。
    schema_: Any = Field(
        ...,
        alias="schema",
        description="JSON Schema 2020-12 契约（对象或布尔）",
    )

    @field_validator("schema_")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, (dict, bool)):
            raise ValueError("schema 必须是 JSON 对象或布尔值（JSON Schema 2020-12）")
        return v


# ---------------------------------------------------------------- 原子发布


class ReleaseItem(StrictModel):
    document_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._~-]*$",
        description="目标文档 ID",
    )
    base_revision: int = Field(
        ..., ge=1, description="来稿所基于的修订号（文档读取时返回）"
    )
    patch: Any = Field(..., description="RFC 7386 JSON Merge Patch")
    guards: Optional[list[Guard]] = Field(
        default=None,
        description="可选前置条件：写事务内先按输入顺序检查当前修订，全部通过才合并",
    )
    author: Optional[str] = Field(default=None, max_length=200)
    lease_token: Optional[int] = Field(
        default=None,
        ge=1,
        description="写入租约 token：目标文档存在未过期租约时必须等于当前 fencing token",
    )

    @field_validator("patch")
    @classmethod
    def _json_only(cls, v: Any) -> Any:
        if not isinstance(v, _JSON_TYPES):
            raise ValueError("patch 必须是合法的 JSON 值")
        return v


class CreateRelease(StrictModel):
    release_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="调用方生成的发布去重 ID（如 UUID），整批请求幂等",
    )
    updates: list[ReleaseItem] = Field(
        ...,
        min_length=2,
        max_length=20,
        description="同一写事务内原子应用的文档更新列表，每项 document_id 互不重复",
    )

    @model_validator(mode="after")
    def _document_ids_unique(self) -> "CreateRelease":
        seen: set[str] = set()
        duplicates: list[str] = []
        for item in self.updates:
            if item.document_id in seen and item.document_id not in duplicates:
                duplicates.append(item.document_id)
            seen.add(item.document_id)
        if duplicates:
            raise ValueError(
                f"updates 中的 document_id 必须互不重复，重复项: {', '.join(duplicates)}"
            )
        return self


# ---------------------------------------------------------------- 写入租约


class AcquireLease(StrictModel):
    holder: str = Field(
        ..., min_length=1, max_length=200, description="租约持有者标识（如服务名 / 工单号）"
    )
    ttl_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        description="租约时长（秒，支持小数），从本次获取 / 续期成功起算",
    )


class RenewLease(StrictModel):
    holder: str = Field(..., min_length=1, max_length=200, description="租约持有者标识")
    lease_token: int = Field(
        ..., ge=1, description="获取租约时返回的 fencing token，须与当前租约匹配"
    )
    ttl_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        description="续期时长（秒，支持小数），从续期成功起算",
    )


class ReleaseLease(StrictModel):
    holder: str = Field(..., min_length=1, max_length=200, description="租约持有者标识")
    lease_token: int = Field(
        ..., ge=1, description="获取租约时返回的 fencing token，须与当前租约匹配"
    )
