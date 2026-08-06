"""ToolRegistry只读意图解析的零副作用合同。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_harness.artifacts import FileArtifactStore
from agent_harness.identity import IdentityContext
from agent_harness.models import ModelAttemptEvidence, compile_output_schema_definition
from agent_harness.models.structured import structured_digest
from agent_harness.models.tool_catalog import ToolCatalog
from agent_harness.models.tool_intent import (
    ProviderToolIntentCandidate,
    ToolIntent,
    build_tool_catalog,
    normalize_provider_tool_intent,
)
from agent_harness.tools import BuiltinTool, ToolRegistry, ToolRuntimeContext
from agent_harness.tools.types import ToolIntentResolutionError

_LOOP_ID = "1" * 64
_USAGE_ID = "2" * 64
_VALIDATION_LOGGER = "agent_harness.tools.registry.validation"


@dataclass
class _SideEffects:
    """聚合所有resolve前后必须保持为零的协作者计数。"""

    handler: int = 0
    preflight: int = 0
    policy: int = 0
    audit: int = 0


class _CountingPolicy:
    def __init__(self, effects: _SideEffects) -> None:
        self.effects = effects

    async def evaluate(self, check: object) -> object:
        """若resolve错误触发Policy，计数会让测试立即暴露越界。"""

        del check
        self.effects.policy += 1
        raise AssertionError("resolve_intent must not evaluate policy")


class _CountingAudit:
    def __init__(self, effects: _SideEffects) -> None:
        self.effects = effects

    async def record(self, **values: object) -> None:
        """若resolve写执行审计，计数会证明其不再是只读验证。"""

        del values
        self.effects.audit += 1
        raise AssertionError("resolve_intent must not write execution audit")


def _registry(tmp_path: Path, effects: _SideEffects) -> ToolRegistry:
    """注册带显式schema identity的纯进程内工具。"""

    def preflight(arguments: dict[str, Any]) -> None:
        del arguments
        effects.preflight += 1

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        effects.handler += 1
        return arguments

    return ToolRegistry(
        tools=[
            BuiltinTool(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
                input_schema_ref="search-input",
                input_schema_version="v1",
                preflight=preflight,
                handler=handler,
            )
        ],
        policy=_CountingPolicy(effects),  # type: ignore[arg-type]
        audit=_CountingAudit(effects),  # type: ignore[arg-type]
        artifact_store=FileArtifactStore(tmp_path / "artifacts"),
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
    )


def _intent_and_catalog(registry: ToolRegistry) -> tuple[ToolIntent, ToolCatalog]:
    """从Registry只读descriptor与provider candidate构造同一冻结绑定。"""

    catalog = build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=registry.catalog_descriptors(),
        selection=None,
    )
    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        schema_ref="search-input",
        version="v1",
    )
    candidate = ProviderToolIntentCandidate(
        provider="provider-a",
        model="model-a",
        tool_name="search",
        arguments={"q": "agent harness"},
        tool_schema_ref=schema.identity.schema_ref,
        tool_schema_version=schema.identity.version,
        tool_schema_digest=schema.identity.digest,
        attempts=[
            ModelAttemptEvidence(
                attempt=1,
                side_effect_state="started",
                outcome="completed",
                completion_observed=True,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
            )
        ],
    )
    intent = normalize_provider_tool_intent(
        candidate,
        expected_provider="provider-a",
        expected_model="model-a",
        expected_tool_name="search",
        expected_tool_schema_ref=schema.identity.schema_ref,
        expected_tool_schema_version=schema.identity.version,
        expected_tool_schema_digest=schema.identity.digest,
        loop_id=_LOOP_ID,
        turn_ordinal=1,
        model_usage_call_id=_USAGE_ID,
        catalog_digest=catalog.catalog_digest,
    )
    return intent, catalog


def _context() -> ToolRuntimeContext:
    """构造不含外部能力的本地执行身份。"""

    return ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="examples.basic",
        run_id="run-a",
        request_id="request-a",
        trace_id="trace-a",
    )


def _assert_redacted_validation_summary(
    caplog: pytest.LogCaptureFixture,
    *,
    code: str,
    intent: ToolIntent,
) -> None:
    """校验证据只能包含稳定错误码和不可逆关联身份，不得泄漏工具输入。"""

    records = [record for record in caplog.records if record.name == _VALIDATION_LOGGER]
    assert len(records) == 1
    assert json.loads(records[0].getMessage()) == {
        "action": "tool.intent.validation",
        "catalog_digest": intent.catalog_digest,
        "code": code,
        "loop_id": intent.loop_id,
        "tool_call_id": intent.tool_call_id,
        "turn_ordinal": intent.turn_ordinal,
    }
    evidence = records[0].getMessage()
    assert "agent harness" not in evidence
    assert '"q"' not in evidence
    assert "search-input" not in evidence
    assert "tool_name" not in evidence


def test_resolve_intent_returns_data_only_binding_with_zero_side_effects(tmp_path: Path) -> None:
    """成功resolve只返回身份、参数、schema、action/resource，不暴露可执行对象。"""

    effects = _SideEffects()
    registry = _registry(tmp_path, effects)
    intent, catalog = _intent_and_catalog(registry)

    resolved = registry.resolve_intent(intent, catalog=catalog)

    assert resolved.tool_call_id == intent.tool_call_id
    assert resolved.action == "tool.search"
    assert resolved.resource == "tool:search"
    assert resolved.arguments == {"q": "agent harness"}
    assert not any(hasattr(resolved, field) for field in ("handler", "preflight", "client"))
    assert effects == _SideEffects()


@pytest.mark.parametrize(
    "mutation,expected_code",
    [
        ({"tool_name": "unknown"}, "tool.not_found"),
        (
            {"arguments": {"q": 7}, "arguments_digest": structured_digest({"q": 7})},
            "tool.schema_validation_failed",
        ),
        ({"tool_schema_version": "v2"}, "model.tool_catalog_conflict"),
        ({"catalog_digest": "9" * 64}, "model.tool_catalog_conflict"),
    ],
)
def test_resolve_intent_fails_closed_without_execution_side_effects(
    tmp_path: Path,
    mutation: dict[str, object],
    expected_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """无效意图在执行协作者前拒绝，只写稳定脱敏validation摘要。"""

    effects = _SideEffects()
    registry = _registry(tmp_path, effects)
    intent, catalog = _intent_and_catalog(registry)
    mutated = intent.model_copy(update=mutation, deep=True)
    caplog.set_level("WARNING", logger=_VALIDATION_LOGGER)

    with pytest.raises(ToolIntentResolutionError) as failure:
        registry.resolve_intent(mutated, catalog=catalog)

    assert failure.value.code == expected_code
    assert effects == _SideEffects()
    _assert_redacted_validation_summary(caplog, code=expected_code, intent=mutated)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"arguments": {"q": 7}, "arguments_digest": structured_digest({"q": 7})},
        {"action": "tool.write"},
        {"tool_schema_version": "v2"},
    ],
)
async def test_call_revalidates_resolved_intent_before_policy_or_handler(
    tmp_path: Path,
    mutation: dict[str, object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """resolve后参数、action或schema被篡改时，普通执行必须零副作用拒绝。"""

    effects = _SideEffects()
    registry = _registry(tmp_path, effects)
    intent, catalog = _intent_and_catalog(registry)
    resolved = registry.resolve_intent(intent, catalog=catalog)
    tampered = resolved.model_copy(update=mutation, deep=True)
    caplog.set_level("WARNING", logger=_VALIDATION_LOGGER)

    with pytest.raises(ToolIntentResolutionError) as failure:
        await registry.call(
            tampered,
            context=_context(),
            intent=intent,
            catalog=catalog,
        )

    assert failure.value.code == "model.tool_catalog_conflict"
    assert effects == _SideEffects()
    _assert_redacted_validation_summary(
        caplog,
        code="model.tool_catalog_conflict",
        intent=intent,
    )


@pytest.mark.asyncio
async def test_call_approved_revalidates_before_claim_or_handler(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Approved入口也必须在创建executor/claim前拒绝篡改后的解析结果。"""

    effects = _SideEffects()
    registry = _registry(tmp_path, effects)
    intent, catalog = _intent_and_catalog(registry)
    resolved = registry.resolve_intent(intent, catalog=catalog)
    tampered = resolved.model_copy(update={"resource": "tool:other"}, deep=True)
    caplog.set_level("WARNING", logger=_VALIDATION_LOGGER)

    with pytest.raises(ToolIntentResolutionError) as failure:
        await registry.call_approved(
            tampered,
            context=_context(),
            grant=object(),  # type: ignore[arg-type]
            intent=intent,
            catalog=catalog,
        )

    assert failure.value.code == "model.tool_catalog_conflict"
    assert effects == _SideEffects()
    _assert_redacted_validation_summary(
        caplog,
        code="model.tool_catalog_conflict",
        intent=intent,
    )
