"""受控模型工具循环复用正式 Policy/Registry 的三态合同。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.model_tool_loop_contract_helpers import (
    initial_model_tool_loop_snapshot,
)
from tests.contracts.run_trace_contract_helpers import seed_persisted_run
from tests.contracts.test_policy_gated_model_tool_loop_public_seam_contracts import (
    ScriptedModelTurns,
    ScriptStep,
    model_loop_limits_fixture,
    model_policy_fixture,
    tool_intent_request_fixture,
)
from tests.contracts.test_tool_registry_intent_resolution_contracts import (
    _intent_and_catalog,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.audit import AuditService
from agent_harness.context import ContextAssemblyService
from agent_harness.identity import IdentityContext
from agent_harness.models import ToolCatalog, build_tool_catalog
from agent_harness.models.structured import structured_digest
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import (
    BoundModelToolLoopService,
    ModelToolLoopError,
    ModelToolLoopService,
    build_execution_context,
)
from agent_harness.storage import ModelToolLoopCreate, SQLAlchemyStorage, run_migrations
from agent_harness.tools import (
    BuiltinTool,
    ToolErrorCode,
    ToolRegistry,
    ToolRuntimeContext,
)
from agent_harness.tools.types import ToolIntentResolutionError

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
    "additionalProperties": False,
}


def _bound_real_policy_loop(
    *,
    storage: SQLAlchemyStorage,
    tmp_path: Path,
    decision: Literal["allow", "deny", "require_approval"],
    handler_effects: list[dict[str, Any]],
    preflight_effects: list[dict[str, Any]],
    run_id: str,
    trace_id: str,
    registry_sink: list[ToolRegistry] | None = None,
) -> tuple[BoundModelToolLoopService, ScriptedModelTurns]:
    """组合真实PolicyEngine/ToolRegistry，只用fake model与进程内计数工具。"""

    deny_actions: set[str] = {"tool.search"} if decision == "deny" else set()
    approval_actions: set[str] = {"tool.search"} if decision == "require_approval" else set()
    audit = AuditService(storage=storage)
    policy = PolicyEngine(
        provider=YamlPolicyProvider(
            deny_actions=deny_actions,
            require_approval_actions=approval_actions,
        ),
        audit=audit,
    )

    def handler(arguments: dict[str, Any]) -> dict[str, str]:
        """唯一业务副作用是追加计数，便于证明拒绝和等待路径为零。"""

        handler_effects.append(dict(arguments))
        return {"value": str(arguments["q"])}

    def preflight(arguments: dict[str, Any]) -> None:
        """记录真实 Registry 预检调用，证明拒绝路径在该 seam 前关闭。"""

        preflight_effects.append(dict(arguments))

    artifact_store = FileArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=_INPUT_SCHEMA,
                input_schema_ref="tools.search.input",
                input_schema_version="v1",
                handler=handler,
                preflight=preflight,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )
    if registry_sink is not None:
        registry_sink.append(registry)
    catalog: ToolCatalog = build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=registry.catalog_descriptors(),
        selection=None,
    )
    script = (
        (ScriptStep("tool_intent"), ScriptStep("final_text"))
        if decision == "allow"
        else (ScriptStep("tool_intent"),)
    )
    model = ScriptedModelTurns(script, storage=storage)
    service = ModelToolLoopService(
        model_turns=model,
        tool_catalog_resolver=lambda _agent_id, _selection: catalog,
        tool_registry_resolver=lambda _agent_id, _tool_name: registry,
        context_assembly=ContextAssemblyService(
            storage=storage,
            artifact_store=artifact_store,
        ),
        loop_limits_resolver=lambda _agent_id: model_loop_limits_fixture(),
        agent_model_policy_resolver=lambda _agent_id: model_policy_fixture(),
        storage=storage,
        artifact_store=artifact_store,
    )
    execution = build_execution_context(
        identity=IdentityContext.local_default(session_id=f"policy-{decision}"),
        services={"model_tool_loop": service},
        agent_id="agent-a",
        run_id=run_id,
        request_id=f"request-{decision}",
        trace_id=trace_id,
    )
    bound = execution.require_service("model_tool_loop")
    assert isinstance(bound, BoundModelToolLoopService)
    return bound, model


@pytest.mark.asyncio
async def test_initial_loop_registry_drift_writes_redacted_validation_before_side_effects(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """目录冻结后Registry撤权时，首次执行只留脱敏摘要且不进入Policy/claim/tool。"""

    dsn = sqlite_dsn(tmp_path / "initial-registry-drift.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    handler_effects: list[dict[str, Any]] = []
    preflight_effects: list[dict[str, Any]] = []
    registries: list[ToolRegistry] = []
    run_id = await seed_persisted_run(
        storage,
        trace_id="trace-initial-registry-drift",
        agent_id="agent-a",
    )
    bound, model = _bound_real_policy_loop(
        storage=storage,
        tmp_path=tmp_path,
        decision="allow",
        handler_effects=handler_effects,
        preflight_effects=preflight_effects,
        run_id=run_id,
        trace_id="trace-initial-registry-drift",
        registry_sink=registries,
    )
    assert len(registries) == 1
    registries[0]._agent_tool_allowlist.clear()  # pyright: ignore[reportPrivateUsage]
    caplog.set_level("WARNING", logger="agent_harness.tools.registry.validation")

    try:
        with pytest.raises(ToolIntentResolutionError) as failure:
            await bound.run(
                tool_intent_request_fixture(),
                operation_key="initial-registry-drift",
            )
        assert failure.value.code == "tool.allowlist_denied"
        assert handler_effects == []
        assert preflight_effects == []
        assert len(model.calls) == 1
        async with storage.uow() as uow:
            records = await uow.audit_logs.list_for_tenant("default")
            claims = await uow.tool_invocations.list_by_model_loop(
                tenant_id="default",
                run_id=run_id,
                loop_id=model.calls[0][2],
            )
        assert [record for record in records if record.action == "policy.decision"] == []
        assert claims == []
        validation = [
            record
            for record in caplog.records
            if record.name == "agent_harness.tools.registry.validation"
        ]
        assert len(validation) == 1
        assert (
            validation[0]
            .getMessage()
            .startswith('{"action":"tool.intent.validation","catalog_digest":')
        )
        assert "weather" not in validation[0].getMessage()
        assert "search" not in validation[0].getMessage()
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_error", "expected_handler_count", "expected_model_calls"),
    [
        ("allow", None, 1, 2),
        ("deny", ToolErrorCode.POLICY_DENIED.value, 0, 1),
        (
            "require_approval",
            ToolErrorCode.APPROVAL_REQUIRED.value,
            0,
            1,
        ),
    ],
)
async def test_real_policy_three_states_preserve_evidence_and_zero_execution(
    tmp_path: Path,
    decision: Literal["allow", "deny", "require_approval"],
    expected_error: str | None,
    expected_handler_count: int,
    expected_model_calls: int,
) -> None:
    """三态均留下decision audit；deny/waiting不执行handler或下一模型轮。"""

    dsn = sqlite_dsn(tmp_path / f"policy-{decision}.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    handler_effects: list[dict[str, Any]] = []
    preflight_effects: list[dict[str, Any]] = []
    trace_id = f"trace-policy-{decision}"
    run_id = await seed_persisted_run(storage, trace_id=trace_id, agent_id="agent-a")
    bound, model = _bound_real_policy_loop(
        storage=storage,
        tmp_path=tmp_path,
        decision=decision,
        handler_effects=handler_effects,
        preflight_effects=preflight_effects,
        run_id=run_id,
        trace_id=trace_id,
    )

    try:
        if expected_error is None:
            await bound.run(
                tool_intent_request_fixture(),
                operation_key=f"policy-{decision}",
            )
        else:
            with pytest.raises(ModelToolLoopError) as failure:
                await bound.run(
                    tool_intent_request_fixture(),
                    operation_key=f"policy-{decision}",
                )
            assert failure.value.code == expected_error

        async with storage.uow() as uow:
            records = await uow.audit_logs.list_for_tenant("default")
        policy_records = [record for record in records if record.action == "policy.decision"]
        assert len(policy_records) == 1
        assert policy_records[0].payload["decision"] == decision
        assert policy_records[0].payload["run_id"] == run_id
        assert len(handler_effects) == expected_handler_count
        assert len(preflight_effects) == expected_handler_count
        assert len(model.calls) == expected_model_calls
        async with storage.uow() as uow:
            durable_loop = await uow.model_tool_loops.get("default", model.calls[0][2])
        assert durable_loop is not None
        assert (
            durable_loop.status
            == {
                "allow": "completed",
                "deny": "failed",
                "require_approval": "failed",
            }[decision]
        )
        if decision == "allow":
            tool_call_id = structured_digest(
                {
                    "loop_id": model.calls[0][2],
                    "turn_ordinal": 1,
                    "arguments": {"q": "turn-1"},
                }
            )
            async with storage.uow() as uow:
                claim = await uow.tool_invocations.get_by_tool_call_id(tool_call_id)
            assert claim is not None
            assert claim.execution_state == "completed"
            assert claim.handler_started_at is not None
            assert claim.result_ref is not None
            assert durable_loop.next_turn_ordinal == 3
            assert durable_loop.cumulative_usage.turns_completed == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_real_registry_orders_policy_claim_permit_preflight_handler_and_replays_terminal(
    tmp_path: Path,
) -> None:
    """真实Registry用SQLite证明安全顺序，终态重放不得再进预检或handler。"""

    database_path = tmp_path / "registry-order-replay.db"
    dsn = sqlite_dsn(database_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    trace_id = "trace-registry-order-replay"
    run_id = await seed_persisted_run(storage, trace_id=trace_id, agent_id="agent-a")
    observations: list[str] = []
    artifact_store = FileArtifactStore(tmp_path / "registry-order-artifacts")
    audit = AuditService(storage=storage)
    policy = PolicyEngine(provider=YamlPolicyProvider(), audit=audit)

    def preflight(_arguments: dict[str, Any]) -> None:
        """在执行边界内读取耐久事实，锁定Policy审计和permit均先于预检。"""

        observations.append("preflight")
        with sqlite3.connect(database_path) as connection:
            policy_count = connection.execute(
                "select count(*) from audit_logs where action = ?",
                ("policy.decision",),
            ).fetchone()
            claim = connection.execute(
                "select handler_started_at from tool_invocations where tool_call_id = ?",
                (intent.tool_call_id,),
            ).fetchone()
        assert policy_count == (1,)
        assert claim is not None
        assert claim[0] is not None

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        """受控副作用计数器用于证明terminal replay不重复真实工具执行。"""

        observations.append("handler")
        return {"value": arguments["q"]}

    registry = ToolRegistry(
        tools=[
            BuiltinTool(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=_INPUT_SCHEMA,
                input_schema_ref="search-input",
                input_schema_version="v1",
                handler=handler,
                preflight=preflight,
            )
        ],
        policy=policy,
        audit=audit,
        artifact_store=artifact_store,
        agent_tool_allowlist=["search"],
        enforce_agent_tool_allowlist=True,
        storage=storage,
    )
    intent, catalog = _intent_and_catalog(registry)
    resolved = registry.resolve_intent(intent, catalog=catalog)
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
        await uow.commit()
    context = ToolRuntimeContext(
        actor=IdentityContext.local_default(session_id="registry-order-replay"),
        agent_id="agent-a",
        run_id=run_id,
        request_id="request-registry-order-replay",
        trace_id=trace_id,
    )

    try:
        with pytest.raises(ToolIntentResolutionError):
            await registry.call(
                resolved.model_copy(update={"action": "tool.write"}),
                context=context,
                intent=intent,
                catalog=catalog,
            )
        assert observations == []

        completed = await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        assert completed.status == "completed"
        assert observations == ["preflight", "handler"]

        replayed = await registry.call(
            resolved,
            context=context,
            intent=intent,
            catalog=catalog,
        )
        assert replayed == completed
        assert observations == ["preflight", "handler"]
        async with storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_tool_call_id(intent.tool_call_id)
        assert claim is not None
        assert claim.execution_state == "completed"
        assert claim.result_ref is not None
    finally:
        await storage.dispose()
