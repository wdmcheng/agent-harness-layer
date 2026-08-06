"""模型工具目录的 exact 选择、冻结投影与 canonical provider bytes。"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_harness.config.model_catalog import MAX_BUDGET_INTEGER
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.models.structured import OutputSchemaDefinition, structured_digest


class ToolCatalogConflictError(RuntimeError):
    """目录选择、授权或 Registry 快照不一致时的稳定关闭失败。"""

    code = "model.tool_catalog_conflict"

    def __init__(self) -> None:
        """错误不回显工具参数或 schema body，只暴露稳定 code。"""

        super().__init__(self.code)


class ToolCatalogSelection(HarnessDTO):
    """业务调用可提交的独立 exact 保序缩权 DTO。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    tool_names: tuple[str, ...]

    @field_validator("tool_names", mode="before")
    @classmethod
    def validate_exact_tuple(cls, value: object) -> object:
        """拒绝 list coercion 与非 string 元素，区分显式空 tuple 和缺省。"""

        if type(value) is not tuple:
            raise ValueError("tool catalog selection must be a string tuple")
        values = cast(tuple[object, ...], value)
        if any(type(item) is not str for item in values):
            raise ValueError("tool catalog selection must be a string tuple")
        return cast(tuple[str, ...], values)


class ToolCatalogSourceDescriptor(HarnessDTO):
    """Registry 提供的只读工具事实；不携带 handler、policy 或 client。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    name: str = Field(min_length=1)
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    input_schema: OutputSchemaDefinition
    registry_ordinal: int = Field(ge=0, strict=True)


class ToolCatalogEntry(HarnessDTO):
    """冻结目录中的一个工具身份与 provider schema 来源。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    name: str = Field(min_length=1)
    input_schema_ref: str = Field(min_length=1)
    input_schema_version: str = Field(min_length=1)
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    ordinal: int = Field(ge=0, strict=True)
    input_schema: OutputSchemaDefinition = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_schema_identity(self) -> ToolCatalogEntry:
        """投影字段必须逐值匹配同一严格 schema definition。"""

        identity = self.input_schema.identity
        if (
            self.input_schema_ref != identity.schema_ref
            or self.input_schema_version != identity.version
            or self.input_schema_digest != identity.digest
        ):
            raise ValueError("tool catalog schema identity does not match definition")
        return self


def _catalog_identity_payload(entries: tuple[ToolCatalogEntry, ...]) -> dict[str, object]:
    """构造不含 schema body 的授权目录身份；provider bytes另行完整冻结body。"""

    return {
        "schema_version": "tool-catalog-v1",
        "tools": [
            {
                "name": item.name,
                "input_schema_ref": item.input_schema_ref,
                "input_schema_version": item.input_schema_version,
                "input_schema_digest": item.input_schema_digest,
                "action": item.action,
                "resource": item.resource,
                "ordinal": item.ordinal,
            }
            for item in entries
        ],
    }


class ToolCatalog(HarnessDTO):
    """Agent授权与Registry事实交集形成的不可变有序目录。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["tool-catalog-v1"] = "tool-catalog-v1"
    tools: tuple[ToolCatalogEntry, ...]
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity(self) -> ToolCatalog:
        """名称唯一、ordinal连续且摘要可从exact授权身份逐值复算。"""

        if len({item.name for item in self.tools}) != len(self.tools):
            raise ValueError("tool catalog names must be unique")
        if tuple(item.ordinal for item in self.tools) != tuple(range(len(self.tools))):
            raise ValueError("tool catalog ordinals must be continuous")
        if structured_digest(_catalog_identity_payload(self.tools)) != self.catalog_digest:
            raise ValueError("tool catalog digest does not match canonical identity")
        return self


class ToolIntentRequestIdentity(HarnessDTO):
    """Tool-enabled route、approval与replay共同绑定的exact请求身份。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    schema_version: Literal["tool-intent-request-identity-v1"] = "tool-intent-request-identity-v1"
    request_shape_ref: Literal["single-user-text-with-tool-catalog"] = (
        "single-user-text-with-tool-catalog"
    )
    request_shape_version: Literal["v1"] = "v1"
    model_catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_catalog_utf8_bytes: int = Field(
        ge=0,
        le=MAX_BUDGET_INTEGER,
        strict=True,
    )
    max_tool_catalog_utf8_bytes: int = Field(
        ge=0,
        le=MAX_BUDGET_INTEGER,
        strict=True,
    )
    trusted_input_token_bound: int = Field(
        ge=0,
        le=MAX_BUDGET_INTEGER,
        strict=True,
    )
    output_token_cap: int = Field(
        ge=1,
        le=MAX_BUDGET_INTEGER,
        strict=True,
    )

    @model_validator(mode="after")
    def validate_catalog_bound(self) -> ToolIntentRequestIdentity:
        """实际canonical bytes不得超过冻结model catalog上限。"""

        if self.tool_catalog_utf8_bytes > self.max_tool_catalog_utf8_bytes:
            raise ValueError("provider tool catalog exceeds frozen byte bound")
        return self

    @property
    def digest(self) -> str:
        """按provider catalog相同canonical JSON规则计算请求身份摘要。"""

        return structured_digest(self.model_dump(mode="json"))


def build_tool_catalog(
    *,
    allowed_tools: tuple[str, ...],
    registry_descriptors: tuple[ToolCatalogSourceDescriptor, ...],
    selection: ToolCatalogSelection | None,
) -> ToolCatalog:
    """按Agent顺序构造目录，并只接受缺省、显式空或唯一保序子序列。"""

    if (
        type(allowed_tools) is not tuple
        or any(type(item) is not str or not item for item in allowed_tools)
        or len(set(allowed_tools)) != len(allowed_tools)
        or type(registry_descriptors) is not tuple
    ):
        raise ToolCatalogConflictError
    snapshots: list[ToolCatalogSourceDescriptor] = []
    try:
        for value in registry_descriptors:
            if type(value) is not ToolCatalogSourceDescriptor:
                raise ValueError
            snapshots.append(
                ToolCatalogSourceDescriptor.model_validate(
                    ToolCatalogSourceDescriptor.model_dump(value, mode="python")
                ).model_copy(deep=True)
            )
    except (AttributeError, TypeError, ValueError):
        raise ToolCatalogConflictError from None
    by_name: dict[str, ToolCatalogSourceDescriptor] = {}
    for descriptor in snapshots:
        if descriptor.name in by_name:
            raise ToolCatalogConflictError
        by_name[descriptor.name] = descriptor
    if any(name not in by_name for name in allowed_tools):
        raise ToolCatalogConflictError

    selected_names = allowed_tools if selection is None else selection.tool_names
    if len(set(selected_names)) != len(selected_names) or any(
        name not in allowed_tools for name in selected_names
    ):
        raise ToolCatalogConflictError
    positions = tuple(allowed_tools.index(name) for name in selected_names)
    if positions != tuple(sorted(positions)):
        raise ToolCatalogConflictError

    entries = tuple(
        ToolCatalogEntry(
            name=name,
            input_schema_ref=by_name[name].input_schema.identity.schema_ref,
            input_schema_version=by_name[name].input_schema.identity.version,
            input_schema_digest=by_name[name].input_schema.identity.digest,
            action=by_name[name].action,
            resource=by_name[name].resource,
            ordinal=ordinal,
            input_schema=by_name[name].input_schema,
        )
        for ordinal, name in enumerate(selected_names)
    )
    return ToolCatalog(
        tools=entries,
        catalog_digest=structured_digest(_catalog_identity_payload(entries)),
    )


def provider_tool_catalog_bytes(catalog: ToolCatalog) -> bytes:
    """生成`provider-tool-catalog-v1`唯一UTF-8 bytes，不读取current Registry。"""

    try:
        if type(catalog) is not ToolCatalog:
            raise ValueError
        entries = tuple(
            ToolCatalogEntry(
                name=item.name,
                input_schema_ref=item.input_schema_ref,
                input_schema_version=item.input_schema_version,
                input_schema_digest=item.input_schema_digest,
                action=item.action,
                resource=item.resource,
                ordinal=item.ordinal,
                input_schema=OutputSchemaDefinition.model_validate(
                    OutputSchemaDefinition.model_dump(item.input_schema, mode="python")
                ),
            )
            for item in catalog.tools
            if type(item) is ToolCatalogEntry
        )
        if len(entries) != len(catalog.tools):
            raise ValueError
        snapshot = ToolCatalog(
            schema_version=catalog.schema_version,
            tools=entries,
            catalog_digest=catalog.catalog_digest,
        )
    except (AttributeError, TypeError, ValueError):
        raise ToolCatalogConflictError from None
    payload = {
        "schema_version": "provider-tool-catalog-v1",
        "tools": [
            {
                "name": item.name,
                "input_schema_ref": item.input_schema_ref,
                "input_schema_version": item.input_schema_version,
                "input_schema_digest": item.input_schema_digest,
                "input_schema": item.input_schema.schema_definition,
                "ordinal": item.ordinal,
            }
            for item in snapshot.tools
        ],
    }
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ToolCatalogConflictError from None


def provider_tool_catalog_digest(catalog: ToolCatalog) -> str:
    """返回冻结provider bytes的SHA-256，供route/snapshot/approval/replay绑定。"""

    return hashlib.sha256(provider_tool_catalog_bytes(catalog)).hexdigest()


__all__ = [
    "ToolCatalog",
    "ToolCatalogConflictError",
    "ToolCatalogEntry",
    "ToolCatalogSelection",
    "ToolCatalogSourceDescriptor",
    "ToolIntentRequestIdentity",
    "build_tool_catalog",
    "provider_tool_catalog_bytes",
    "provider_tool_catalog_digest",
]
