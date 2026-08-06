"""completed工具结果与untrusted Context Assembly的精确重放合同。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run
from tests.contracts.test_tool_registry_intent_resolution_contracts import (
    _intent_and_catalog,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.context import ContextAssemblyService, ContextFragment
from agent_harness.identity import IdentityContext
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.storage import ModelToolLoopCreate, SQLAlchemyStorage, run_migrations
from agent_harness.tools import BuiltinTool, ToolRegistry, ToolRuntimeContext
from agent_harness.tools.durable_execution import build_model_tool_invocation_claim


@pytest.mark.asyncio
async def test_completed_result_replays_same_untrusted_context_without_handler(
    tmp_path: Path,
) -> None:
    """同一tool result只执行一次，并复用同一assembly identity/output摘要。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'exact-result.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    effects: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        effects.append(dict(arguments))
        return {"value": arguments["q"]}

    registry = ToolRegistry(
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
                handler=handler,
            )
        ],
        policy=PolicyEngine(provider=YamlPolicyProvider()),
        audit=None,
        artifact_store=artifacts,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )
    intent, catalog = _intent_and_catalog(registry)
    resolved = registry.resolve_intent(intent, catalog=catalog)
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="agent-a",
        run_id="placeholder",
        request_id="request-a",
        trace_id="trace-exact-result",
    )
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-exact-result")
        context = context.model_copy(update={"run_id": run_id})
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="default",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=intent.loop_id,
                    request_identity_digest="a" * 64,
                    operation_identity_digest="b" * 64,
                    catalog_digest=catalog.catalog_digest,
                    **initial_model_tool_loop_snapshot(),
                    owner_lease_digest="c" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
            await uow.commit()

        first = await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        replay = await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        assert replay == first
        assert effects == [{"q": "agent harness"}]

        content = json.dumps(first.result, sort_keys=True, separators=(",", ":"))
        fragment = ContextFragment(
            source_ref=first.source_ref,
            trust_level="untrusted",
            content=content,
            token_estimate=4,
            kind="tool_result",
            artifact_ref=first.artifact_ref,
        )
        service = ContextAssemblyService(storage=storage, artifact_store=artifacts)
        assembly = await service.assemble(
            tenant_id="default",
            run_id=run_id,
            fragments=[fragment],
            token_budget=16,
            loop_id=intent.loop_id,
            turn_ordinal=intent.turn_ordinal,
            tool_call_id=intent.tool_call_id,
        )
        replayed_assembly = await service.assemble(
            tenant_id="default",
            run_id=run_id,
            fragments=[fragment],
            token_budget=16,
            loop_id=intent.loop_id,
            turn_ordinal=intent.turn_ordinal,
            tool_call_id=intent.tool_call_id,
        )
        assert replayed_assembly.id == assembly.id
        assert replayed_assembly.output_ref == assembly.output_ref
        assert replayed_assembly.assembled_text == assembly.assembled_text
        async with storage.uow() as uow:
            persisted = await uow.context_assemblies.get(assembly.id)
        assert persisted is not None
        assert persisted.input_identity_digest is not None
        assert persisted.output_digest is not None
        assert persisted.tool_call_id == intent.tool_call_id
        with pytest.raises(RuntimeError) as failure:
            await service.assemble(
                tenant_id="default",
                run_id=run_id,
                fragments=[fragment.model_copy(update={"content": '{"value":"tampered"}'})],
                token_budget=16,
                loop_id=intent.loop_id,
                turn_ordinal=intent.turn_ordinal,
                tool_call_id=intent.tool_call_id,
            )
        assert getattr(failure.value, "code", None) == "context.assembly_replay_conflict"
        assert effects == [{"q": "agent harness"}]
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_public_registry_rotates_fence_for_expired_unstarted_claim(
    tmp_path: Path,
) -> None:
    """普通Registry恢复过期claim时必须自行派生后继fence，并且handler仅执行一次。"""

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'registry-claim-takeover.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    artifacts = FileArtifactStore(tmp_path / "artifacts")
    effects: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        """副作用计数器证明恢复赢家执行一次，过期owner没有执行handler。"""

        effects.append(dict(arguments))
        return {"value": arguments["q"]}

    registry = ToolRegistry(
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
                handler=handler,
            )
        ],
        policy=PolicyEngine(provider=YamlPolicyProvider()),
        audit=None,
        artifact_store=artifacts,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )
    intent, catalog = _intent_and_catalog(registry)
    resolved = registry.resolve_intent(intent, catalog=catalog)
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(),
        agent_id="agent-a",
        run_id="placeholder",
        request_id="request-a",
        trace_id="trace-registry-takeover",
    )
    try:
        run_id = await seed_persisted_run(storage, trace_id="trace-registry-takeover")
        context = context.model_copy(update={"run_id": run_id})
        async with storage.uow() as uow:
            await uow.model_tool_loops.create(
                ModelToolLoopCreate(
                    tenant_id="default",
                    run_id=run_id,
                    agent_id="agent-a",
                    loop_id=intent.loop_id,
                    request_identity_digest="a" * 64,
                    operation_identity_digest="b" * 64,
                    catalog_digest=catalog.catalog_digest,
                    **initial_model_tool_loop_snapshot(),
                    owner_lease_digest="c" * 64,
                    owner_fence=1,
                    owner_lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            # 人工播种唯一允许的崩溃窗口：claim已提交，但handler尚未开始。
            # 恢复调用仍必须从Registry公共入口自行读取旧fence并执行CAS换租。
            expired_claim = build_model_tool_invocation_claim(
                resolved=resolved,
                context=context,
                args_ref=artifacts.reference_json({"arguments": resolved.arguments}).ref,
                approval_id=None,
                now=datetime(2000, 1, 1, tzinfo=UTC),
            )
            await uow.tool_invocations.create_model_claim(expired_claim)
            await uow.commit()

        result = await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=catalog,
        )

        assert result.status == "completed"
        assert effects == [{"q": "agent harness"}]
        async with storage.uow() as uow:
            recovered = await uow.tool_invocations.get_by_tool_call_id(intent.tool_call_id)
        assert recovered is not None
        assert recovered.execution_fence == 2
        assert recovered.execution_state == "completed"
        assert recovered.not_started_proof is not None
        assert recovered.not_started_proof["prior_fence"] == 1
        assert recovered.not_started_proof["next_fence"] == 2
    finally:
        await storage.dispose()
