"""工具目录选择与 provider 请求快照的 provider-neutral 合同。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from agent_harness.models import ModelRequest, ToolCatalog, compile_output_schema_definition
from agent_harness.models.tool_intent import (
    ToolCatalogConflictError,
    ToolCatalogSelection,
    ToolCatalogSourceDescriptor,
    build_tool_catalog,
    provider_tool_catalog_bytes,
)
from agent_harness.runtime.services import AgentToolCatalogResolver

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
    "additionalProperties": False,
}


def _descriptor(
    name: str,
    *,
    ordinal: int,
    action: str | None = None,
) -> ToolCatalogSourceDescriptor:
    """用严格 schema 构造不含 handler 的 Registry 只读描述。"""

    schema = compile_output_schema_definition(
        _SEARCH_SCHEMA,
        schema_ref=f"{name}-input",
        version="v1",
    )
    return ToolCatalogSourceDescriptor(
        name=name,
        action=action or f"tool.{name}",
        resource=f"tool:{name}",
        input_schema=schema,
        registry_ordinal=ordinal,
    )


def _descriptors() -> tuple[ToolCatalogSourceDescriptor, ...]:
    return (
        _descriptor("search", ordinal=7),
        _descriptor("read", ordinal=2),
        _descriptor("write", ordinal=9),
        _descriptor("registry-only", ordinal=1),
    )


def test_tool_selection_is_an_exact_string_tuple_dto() -> None:
    """显式空 tuple 与缺省 None 语义不同，且不接受 list coercion。"""

    assert ToolCatalogSelection(tool_names=()).tool_names == ()
    assert ToolCatalogSelection(tool_names=("search", "write")).tool_names == (
        "search",
        "write",
    )
    with pytest.raises(ValidationError):
        ToolCatalogSelection.model_validate({"tool_names": ["search"]})
    with pytest.raises(ValidationError):
        ToolCatalogSelection.model_validate({"tool_names": ("search", 1)})
    with pytest.raises(ValidationError):
        ToolCatalogSelection.model_validate({"tool_names": (), "extra": True})


def test_catalog_defaults_to_full_agent_order_and_intersects_registry() -> None:
    """Agent allowlist 决定顺序，Registry 额外工具不能扩大授权。"""

    catalog = build_tool_catalog(
        allowed_tools=("search", "read", "write"),
        registry_descriptors=_descriptors(),
        selection=None,
    )

    assert [item.name for item in catalog.tools] == ["search", "read", "write"]
    assert [item.ordinal for item in catalog.tools] == [0, 1, 2]
    assert all(
        item.input_schema_digest == item.input_schema.identity.digest for item in catalog.tools
    )
    assert (
        catalog.catalog_digest
        == build_tool_catalog(
            allowed_tools=("search", "read", "write"),
            registry_descriptors=tuple(reversed(_descriptors())),
            selection=None,
        ).catalog_digest
    )


def test_catalog_distinguishes_explicit_empty_and_ordered_subsequence() -> None:
    """选择只允许保序缩小，并为 provider 重新生成连续 ordinal。"""

    empty = build_tool_catalog(
        allowed_tools=("search", "read", "write"),
        registry_descriptors=_descriptors(),
        selection=ToolCatalogSelection(tool_names=()),
    )
    subset = build_tool_catalog(
        allowed_tools=("search", "read", "write"),
        registry_descriptors=_descriptors(),
        selection=ToolCatalogSelection(tool_names=("search", "write")),
    )

    assert empty.tools == ()
    assert [(item.name, item.ordinal) for item in subset.tools] == [
        ("search", 0),
        ("write", 1),
    ]
    assert empty.catalog_digest != subset.catalog_digest


@pytest.mark.parametrize(
    "tool_names",
    [
        ("search", "unknown"),
        ("search", "search"),
        ("write", "search"),
    ],
)
def test_catalog_rejects_unknown_duplicate_or_reordered_selection(
    tool_names: tuple[str, ...],
) -> None:
    """任何扩权或重排都在 provider、usage claim 与工具副作用前关闭失败。"""

    with pytest.raises(ToolCatalogConflictError) as failure:
        build_tool_catalog(
            allowed_tools=("search", "read", "write"),
            registry_descriptors=_descriptors(),
            selection=ToolCatalogSelection(tool_names=tool_names),
        )
    assert failure.value.code == "model.tool_catalog_conflict"


def test_catalog_rejects_missing_duplicate_or_drifting_registry_descriptor() -> None:
    """Agent列出的每个工具必须在同一Registry快照中恰有一个精确描述。"""

    for descriptors in (
        (_descriptor("search", ordinal=0),),
        (_descriptor("search", ordinal=0), _descriptor("search", ordinal=1)),
    ):
        with pytest.raises(ToolCatalogConflictError):
            build_tool_catalog(
                allowed_tools=("search", "read"),
                registry_descriptors=descriptors,
                selection=None,
            )


def test_selection_cannot_be_smuggled_into_model_request() -> None:
    """通用 ModelRequest 保持不变，capability 专属选择只能走独立关键字参数。"""

    with pytest.raises(ValidationError):
        ModelRequest.model_validate(
            {
                "prompt": "use a tool",
                "capability": "tool_intent",
                "tool_selection": {"tool_names": ["search"]},
            }
        )


def test_provider_catalog_matches_frozen_golden_vector_byte_for_byte() -> None:
    """Schema body、字段顺序、空白与 ordinal 均进入唯一 provider bytes。"""

    catalog = build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=(_descriptor("search", ordinal=11),),
        selection=None,
    )
    encoded = provider_tool_catalog_bytes(catalog)
    expected = (
        b'{"schema_version":"provider-tool-catalog-v1","tools":[{"input_schema":'
        b'{"additionalProperties":false,"properties":{"q":{"type":"string"}},'
        b'"required":["q"],"type":"object"},"input_schema_digest":'
        b'"d90ec2f895920b2f26f124f6d07f6115e64e395e36ca80ecc9530c6202f5be29",'
        b'"input_schema_ref":"search-input","input_schema_version":"v1",'
        b'"name":"search","ordinal":0}]}'
    )

    assert encoded == expected
    assert len(encoded) == 352
    assert hashlib.sha256(encoded).hexdigest() == (
        "31bc934ff80b541bd26efb154d97b3ba27ee3e2fdf7b1dcbacb2d6431b940d04"
    )


class _CatalogOnlyRegistry:
    """Resolver合同只提供data-only descriptors，不暴露执行handler。"""

    def catalog_descriptors(self) -> tuple[ToolCatalogSourceDescriptor, ...]:
        return _descriptors()


class _CatalogRegistryFactory:
    """记录组合根传入的descriptor allowlist与请求名，不执行任何工具。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def __call__(
        self,
        *,
        allowed_tools: tuple[str, ...],
        requested_tool_name: str,
    ) -> _CatalogOnlyRegistry:
        self.calls.append((allowed_tools, requested_tool_name))
        return _CatalogOnlyRegistry()


class _CatalogAgentRegistry:
    """模拟加载期已冻结目录的 AgentRegistry 公共 seam。"""

    @staticmethod
    def get(_agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            tool_policy=SimpleNamespace(allowed_tools=["search", "read", "write"])
        )

    @staticmethod
    def resolve_tool_catalog(
        _agent_id: str,
        selection: ToolCatalogSelection | None,
    ) -> ToolCatalog:
        return build_tool_catalog(
            allowed_tools=("search", "read", "write"),
            registry_descriptors=_descriptors(),
            selection=selection,
        )


def test_agent_catalog_resolver_projects_descriptor_order_and_selection() -> None:
    """生产resolver必须从descriptor逐值取allowlist，再与Registry快照求交集。"""

    factory = _CatalogRegistryFactory()

    resolver = AgentToolCatalogResolver(
        registry=cast(Any, _CatalogAgentRegistry()),
        tool_registry_factory=cast(Any, factory),
    )

    full = resolver("agent-a", None)
    empty = resolver("agent-a", ToolCatalogSelection(tool_names=()))
    subset = resolver(
        "agent-a",
        ToolCatalogSelection(tool_names=("search", "write")),
    )

    assert [item.name for item in full.tools] == ["search", "read", "write"]
    assert empty.tools == ()
    assert [item.name for item in subset.tools] == ["search", "write"]
    assert factory.calls == [
        (("search", "read", "write"), ""),
        (("search", "read", "write"), ""),
        (("search", "read", "write"), ""),
    ]


def test_agent_catalog_resolver_rejects_selection_expansion_without_execution() -> None:
    """未知、重复或重排选择只能得到稳定catalog conflict，不能转为工具请求。"""

    factory = _CatalogRegistryFactory()

    resolver = AgentToolCatalogResolver(
        registry=cast(Any, _CatalogAgentRegistry()),
        tool_registry_factory=cast(Any, factory),
    )

    with pytest.raises(ToolCatalogConflictError) as failure:
        resolver(
            "agent-a",
            ToolCatalogSelection(tool_names=("write", "search")),
        )

    assert failure.value.code == "model.tool_catalog_conflict"
    assert factory.calls == []


def test_agent_catalog_resolver_rejects_runtime_action_resource_drift() -> None:
    """加载后 Registry 身份漂移必须在模型调用前关闭，不能改写冻结目录。"""

    class _DriftingFactory:
        def __init__(self) -> None:
            self.calls: list[tuple[tuple[str, ...], str]] = []

        def __call__(
            self,
            *,
            allowed_tools: tuple[str, ...],
            requested_tool_name: str,
        ) -> SimpleNamespace:
            self.calls.append((allowed_tools, requested_tool_name))
            descriptors = list(_descriptors())
            descriptors[0] = descriptors[0].model_copy(
                update={"action": "search.admin", "resource": "search:other"}
            )
            return SimpleNamespace(catalog_descriptors=lambda: tuple(descriptors))

    factory = _DriftingFactory()
    resolver = AgentToolCatalogResolver(
        registry=cast(Any, _CatalogAgentRegistry()),
        tool_registry_factory=cast(Any, factory),
    )

    with pytest.raises(ToolCatalogConflictError):
        resolver("agent-a", None)

    assert factory.calls == [(("search", "read", "write"), "")]
