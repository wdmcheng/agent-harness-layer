"""结构化Policy/HITL身份、恢复与篡改拒绝合同。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run
from tests.contracts.test_provider_neutral_structured_public_seam_contracts import (
    StructuredOutputFixture,
    StructuredProviderDouble,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelApprovalRequired,
    ModelInvocationService,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    OutputSchemaIdentity,
    compile_output_schema,
)
from agent_harness.models._invocation_approval_identity import (
    structured_approval_arguments,
    structured_approval_arguments_bytes,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import ApprovalGrant
from agent_harness.storage import (
    ApprovalCreate,
    CheckpointCreate,
    SQLAlchemyStorage,
    run_migrations,
)


@dataclass
class _ApprovalFixture:
    """保留structured审批测试的公共服务与耐久身份，便于逐项篡改。"""

    service: ModelInvocationService
    storage: SQLAlchemyStorage
    bound: Any
    provider: StructuredProviderDouble
    request: ModelRequest
    approval: Any
    grant: ApprovalGrant
    schema_holder: dict[str, OutputSchemaDefinition]


async def _structured_approval_fixture(tmp_path: Path, *, tamper: str) -> _ApprovalFixture:
    """经公开首次调用生成审批请求，再按场景写入record/checkpoint/lease。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema, candidates=[{"answer": "approved"}])
    schema_holder = {"current": schema}
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'approval-{tamper}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
            providers={"provider-a": provider},  # type: ignore[dict-item]
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / f"approval-{tamper}.jsonl"),
        policy_engine=PolicyEngine(
            provider=YamlPolicyProvider(require_approval_actions=["model.invoke"])
        ),
        output_schema_resolver=lambda agent_id: schema_holder["current"],
    )
    run_id = await seed_run(storage, request_id="request-a")
    bound = service.bind_execution(
        identity=IdentityContext(
            tenant_id="tenant-a",
            user_id="user-a",
            session_id="session-a",
        ),
        tenant_id="tenant-a",
        run_id=run_id,
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
    )
    request = ModelRequest(
        provider="provider-a",
        prompt="return approved answer",
        model="model-a",
        estimated_input_tokens=3,
        max_output_tokens=8,
    )
    with pytest.raises(ModelApprovalRequired) as captured:
        await bound.complete_structured(
            request,
            operation_key="original-structured-slot",
            repair_limit=1,
        )
    approval_request = captured.value.request
    continuation = dict(approval_request.continuation)
    checkpoint_continuation = dict(continuation)
    if tamper == "synchronized_usage_identity":
        continuation["usage_call_id"] = "f" * 64
        checkpoint_continuation = dict(continuation)
    elif tamper == "synchronized_operation_identity":
        continuation["operation_identity_digest"] = "f" * 64
        checkpoint_continuation = dict(continuation)
    elif tamper == "record_extra":
        continuation["unexpected"] = True
        checkpoint_continuation = dict(continuation)
    elif tamper == "record_missing":
        continuation.pop("schema_identity")
        checkpoint_continuation = dict(continuation)
    elif tamper == "checkpoint_mismatch":
        checkpoint_continuation["operation_identity_digest"] = "e" * 64

    resume_token = f"resume-{tamper}"
    identity = IdentityContext(
        tenant_id="tenant-a",
        user_id="user-a",
        session_id="session-a",
    )
    async with storage.uow() as uow:
        await uow.checkpoints.create(
            CheckpointCreate(
                tenant_id="tenant-a",
                run_id=run_id,
                sequence=1,
                resume_token=resume_token,
                state={
                    "kind": "agent_executor_approval",
                    "continuation": checkpoint_continuation,
                },
            )
        )
        record = await uow.approvals.create(
            ApprovalCreate(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                action=approval_request.action,
                resource=approval_request.resource,
                reason=approval_request.reason,
                resume_token=resume_token,
                requested_by=identity.user_id,
                trace_id="trace-a",
                request_id="request-a",
                metadata={
                    "identity_id": identity.user_id,
                    "arguments_hash": approval_request.arguments_hash,
                    "continuation": continuation,
                },
            )
        )
        lease = await uow.approvals.claim_resolution(
            approval_id=record.approval_id,
            run_id=run_id,
            tenant_id="tenant-a",
            request_id="approve-request-a",
        )
        await uow.commit()
    grant = ApprovalGrant(
        approval_id=record.approval_id,
        lease_id=lease.lease_id,
        tenant_id="tenant-a",
        identity_id="user-a",
        session_id="session-a",
        agent_id="agent-a",
        run_id=run_id,
        action="model.invoke",
        resource="agent:agent-a:model",
        arguments_hash=approval_request.arguments_hash,
    )
    return _ApprovalFixture(
        service,
        storage,
        bound,
        provider,
        request,
        approval_request,
        grant,
        schema_holder,
    )


def test_structured_approval_arguments_match_frozen_golden_vector() -> None:
    """审批 hash 必须绑定显式 null、原 request、schema、repair 与两份身份。"""

    arguments = structured_approval_arguments(
        request=ModelRequest(
            prompt="你好",
            capability="structured_output",
            estimated_input_tokens=3,
            max_output_tokens=8,
        ),
        usage_call_id="1" * 64,
        operation_identity_digest="2" * 64,
        schema_identity=OutputSchemaIdentity(
            schema_ref="example.Output",
            version="1",
            digest="0" * 64,
        ),
        repair_limit=1,
    )
    canonical = structured_approval_arguments_bytes(arguments)

    assert len(canonical) == 643
    assert hashlib.sha256(canonical).hexdigest() == (
        "94213e9ecdbbe2e5c50fb565d1ac39462c86e9963c161ba8d2f03b4c5da5efdc"
    )


@pytest.mark.asyncio
async def test_public_require_approval_hash_binds_original_bound_request(
    tmp_path: Path,
) -> None:
    """公开seam的审批hash必须绑定业务请求，不能改绑provider prompt。"""

    fixture = await _structured_approval_fixture(tmp_path, tamper="original-request")
    try:
        continuation = fixture.approval.continuation
        expected_arguments = structured_approval_arguments(
            request=fixture.request,
            usage_call_id=str(continuation["usage_call_id"]),
            operation_identity_digest=str(continuation["operation_identity_digest"]),
            schema_identity=fixture.schema_holder["current"].identity,
            repair_limit=1,
        )
        expected_hash = hashlib.sha256(
            structured_approval_arguments_bytes(expected_arguments)
        ).hexdigest()

        assert fixture.approval.arguments_hash == expected_hash
        assert fixture.provider.sends == []
    finally:
        await fixture.service.aclose()
        await fixture.storage.dispose()


@pytest.mark.asyncio
async def test_structured_approval_golden_vector_and_active_grant_restore_original_identity(
    tmp_path: Path,
) -> None:
    """有效grant忽略调用方operation key，并只在原usage身份下产生一次结果。"""

    fixture = await _structured_approval_fixture(tmp_path, tamper="none")
    try:
        usage_call_id = fixture.approval.continuation["usage_call_id"]
        async with fixture.storage.uow() as uow:
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id=usage_call_id,
                )

        response = await fixture.bound.complete_structured_approved(
            fixture.request,
            operation_key="caller-must-not-rekey",
            repair_limit=1,
            grant=fixture.grant,
        )

        assert response.output_text == '{"answer":"approved"}'
        assert fixture.provider.sends == [(0, 1)]
        async with fixture.storage.uow() as uow:
            persisted = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=usage_call_id,
            )
            persisted_state = persisted.state
        assert persisted_state == "published"
    finally:
        await fixture.service.aclose()
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        "request",
        "repair",
        "schema",
        "grant_hash",
        "lease",
        "synchronized_usage_identity",
        "synchronized_operation_identity",
        "record_extra",
        "record_missing",
        "checkpoint_mismatch",
    ],
)
async def test_structured_approval_tamper_matrix_fails_before_provider(
    tmp_path: Path,
    tamper: str,
) -> None:
    """任一输入、grant、lease或双份continuation漂移都必须零provider失败。"""

    fixture = await _structured_approval_fixture(tmp_path, tamper=tamper)
    request = fixture.request
    repair_limit = 1
    grant = fixture.grant
    if tamper == "request":
        request = request.model_copy(update={"prompt": "tampered"})
    elif tamper == "repair":
        repair_limit = 0
    elif tamper == "grant_hash":
        grant = grant.model_copy(update={"arguments_hash": "d" * 64})
    elif tamper == "lease":
        grant = grant.model_copy(update={"lease_id": "not-active"})
    elif tamper == "schema":
        other_schema = compile_output_schema(
            StructuredOutputFixture,
            schema_ref="agents.example.schemas.Output",
            version="2.0.0",
        )
        fixture.schema_holder["current"] = other_schema
    try:
        with pytest.raises(ValueError):
            await fixture.bound.complete_structured_approved(
                request,
                operation_key="caller-must-not-rekey",
                repair_limit=repair_limit,
                grant=grant,
            )
        assert fixture.provider.sends == []
    finally:
        await fixture.service.aclose()
        await fixture.storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["deny", "approval"])
async def test_policy_deny_and_require_approval_stop_before_claim_or_send(
    tmp_path: Path,
    decision: str,
) -> None:
    """两类model.invoke策略终态都必须位于usage claim与provider之前。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema, candidates=[{"answer": "must-not-send"}])
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'policy-{decision}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    policy_provider = (
        YamlPolicyProvider(deny_actions=["model.invoke"])
        if decision == "deny"
        else YamlPolicyProvider(require_approval_actions=["model.invoke"])
    )
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
            providers={"provider-a": provider},  # type: ignore[dict-item]
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / f"policy-{decision}.jsonl"),
        policy_engine=PolicyEngine(provider=policy_provider),
        output_schema_resolver=lambda agent_id: schema,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        request = ModelRequest(
            provider="provider-a",
            prompt="policy controlled",
            model="model-a",
            estimated_input_tokens=3,
            max_output_tokens=8,
        )
        usage_call_id = "0" * 64
        if decision == "deny":
            from agent_harness.models import ModelProviderInvocationError

            with pytest.raises(ModelProviderInvocationError) as deny_failure:
                await bound.complete_structured(
                    request, operation_key="policy-slot", repair_limit=1
                )
            assert deny_failure.value.code == "model.policy_denied"
        else:
            with pytest.raises(ModelApprovalRequired) as approval_failure:
                await bound.complete_structured(
                    request, operation_key="policy-slot", repair_limit=1
                )
            continuation = approval_failure.value.request.continuation
            assert continuation["kind"] == "structured_policy_approval"
            assert continuation["arguments_hash"] == approval_failure.value.request.arguments_hash
            usage_call_id = str(continuation["usage_call_id"])
        assert provider.sends == []
        async with storage.uow() as uow:
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a", usage_call_id=usage_call_id
                )
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_like_json_does_not_change_model_policy_order_or_execute_tools(
    tmp_path: Path,
) -> None:
    """Tool-like JSON仍只是模型prompt，DENY不得触发任何tool或provider seam。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema, candidates=[{"answer": "must-not-send"}])
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'tool-like-policy.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
            providers={"provider-a": provider},  # type: ignore[dict-item]
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "tool-like-policy.jsonl"),
        policy_engine=PolicyEngine(provider=YamlPolicyProvider(deny_actions=["model.invoke"])),
        output_schema_resolver=lambda agent_id: schema,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a", user_id="user-a", session_id="session-a"
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        from agent_harness.models import ModelProviderInvocationError

        with pytest.raises(ModelProviderInvocationError) as captured:
            await bound.complete_structured(
                ModelRequest(
                    provider="provider-a",
                    prompt='{"tool":"shell.execute","arguments":{"command":"never"}}',
                    model="model-a",
                    max_output_tokens=8,
                ),
                operation_key="tool-like-policy",
            )
        assert captured.value.code == "model.policy_denied"
        assert provider.sends == []
    finally:
        await service.aclose()
        await storage.dispose()
