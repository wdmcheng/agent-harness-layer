"""共享预算 route-chain v2 identity、ORM 与 repository 公共 seam 合同。"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from tests.contracts.test_shared_parent_budget_repository_contracts import (
    create_delegation,
    create_root,
)

from agent_harness.models._route_chain_state import (
    close_route_attempt,
    mark_route_delta_observed,
)
from agent_harness.storage import SQLAlchemyStorage, get_head_revision
from agent_harness.storage.model_route_chain_state import ModelRouteChainState
from agent_harness.storage.run_models import AgentRunModel
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
    DirectBudgetClaim,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)

_CHAIN_USAGE_ID = "b" * 64
CHAIN_USAGE_ID = _CHAIN_USAGE_ID


def _allocation_v2_identity(
    *,
    root_id: str,
    child_id: str,
    delegation_id: str,
) -> OperationIdentity:
    """构造与 allocation snapshot 坐标一致的 route-chain v2 identity。"""

    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"test-only-budget-fingerprint-key",
        fingerprint_key_version="test-v1",
        ownership_kind="allocation",
        run_id=child_id,
        agent_id="agent-b",
        delegation_claim_id=delegation_id,
        usage_kind="model",
        operation_slot=_CHAIN_USAGE_ID,
        semantic_request={"prompt_ref": "route-chain-allocation"},
        tree_snapshot_id=f"snapshot:{root_id}",
        agent_sub_snapshot_id=f"snapshot:{root_id}:agent-b",
        provider="fake",
        model="fake-basic",
        price_source_ref="price:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=20,
        trusted_cost_bound=Decimal("1.00"),
        route_chain_digest="a" * 64,
        route_candidate_count=2,
    )


allocation_v2_identity = _allocation_v2_identity


def _allocation_route_state(*, started: bool = False) -> ModelRouteChainState:
    """构造 allocation 初始 state；可选追加尚未关闭的全局 attempt 1。"""

    payload: dict[str, object] = {
        "schema_version": "model-route-chain-state-v1",
        "chain_id": "a" * 64,
        "candidate_count": 2,
        "usage_call_id": _CHAIN_USAGE_ID,
        "operation_identity_digest": "c" * 64,
        "active_ordinal": 1,
        "waiting_approval_ordinal": None,
        "selected_ordinal": None,
        "evidence_route_ordinal": 1,
        "delta_fenced": False,
        "attempt_lifecycle": (
            [
                {
                    "attempt": 1,
                    "candidate_ordinal": 1,
                    "attempt_identity_digest": "d" * 64,
                    "lifecycle_state": "started",
                    "side_effect_state": "not_started",
                    "request_sent": False,
                    "http_response_observed": False,
                    "http_status": None,
                    "response_identity_observed": False,
                    "usage_observed": False,
                    "text_observed": False,
                    "delta_observed": False,
                    "completion_observed": None,
                    "not_started_proof_digest": None,
                }
            ]
            if started
            else []
        ),
        "current_reservation": {
            "candidate_ordinal": 1,
            "token_bound": 20,
            "cost_bound": 1.0,
        },
        "candidates": [
            {
                "ordinal": ordinal,
                "deployment_id": f"fake-{ordinal}",
                "provider": "fake",
                "model": "fake-basic",
                "route_digest": str(ordinal) * 64,
                "state": "active" if ordinal == 1 else "pending",
                "side_effect_state": "not_started",
                "reason": None,
                "request_sent": False,
                "http_response_observed": False,
                "http_status": None,
                "response_identity_observed": False,
                "usage_observed": False,
                "text_observed": False,
                "delta_observed": False,
                "completion_observed": None,
                "not_started_proofs": [],
                "approval_request_binding_digest": None,
                "approval_grant_binding_digest": None,
            }
            for ordinal in (1, 2)
        ],
        "transitions": [
            {
                "sequence": 1,
                "from_ordinal": None,
                "to_ordinal": 1,
                "state": "activated",
                "reason": "initial",
                "released_token_bound": 0,
                "released_cost_bound": None,
                "reserved_token_bound": 20,
                "reserved_cost_bound": 1.0,
            }
        ],
    }
    return ModelRouteChainState.model_validate(payload)


allocation_route_state = _allocation_route_state


def _second_started_route_state() -> ModelRouteChainState:
    """构造前一 lifecycle 仍为 started 时非法追加 attempt 2 的状态。"""

    started = _allocation_route_state(started=True)
    first = started.attempt_lifecycle[0]
    second = first.model_copy(update={"attempt": 2, "attempt_identity_digest": "9" * 64})
    # 绕过 DTO 重校验，确保仓储边界仍独立拒绝连续 started attempt。
    return started.model_copy(update={"attempt_lifecycle": (first, second)})


second_started_route_state = _second_started_route_state


def _allocation_proven_state() -> ModelRouteChainState:
    """把 allocation attempt 1 以 client-not-started proof 原子关闭。"""

    payload = _allocation_route_state(started=True).to_payload()
    proof_digest = "e" * 64
    proof = {
        "attempt": 1,
        "reason": "client_not_started",
        "side_effect_state": "not_started",
        "request_sent": False,
        "http_response_observed": False,
        "http_status": None,
        "response_identity_observed": False,
        "usage_observed": False,
        "text_observed": False,
        "delta_observed": False,
        "completion_observed": None,
        "endpoint_policy_digest": "f" * 64,
        "classifier_ref": None,
        "classifier_version": None,
        "proof_digest": proof_digest,
    }
    payload["attempt_lifecycle"][0].update(
        {
            "lifecycle_state": "not_started_proven",
            "not_started_proof_digest": proof_digest,
        }
    )
    payload["candidates"][0].update(
        {
            "reason": "client_not_started",
            "not_started_proofs": [proof],
        }
    )
    return ModelRouteChainState.model_validate(payload)


allocation_proven_state = _allocation_proven_state


def _allocation_transferred_state() -> ModelRouteChainState:
    """从完整 proof 前态原子把 allocation reservation 由 A 替换为 B。"""

    payload = _allocation_proven_state().to_payload()
    payload["candidates"][0]["state"] = "not_started"
    payload["candidates"][1]["state"] = "active"
    payload["active_ordinal"] = 2
    payload["evidence_route_ordinal"] = 2
    payload["current_reservation"] = {
        "candidate_ordinal": 2,
        "token_bound": 10,
        "cost_bound": 0.5,
    }
    payload["transitions"].append(
        {
            "sequence": 2,
            "from_ordinal": 1,
            "to_ordinal": 2,
            "state": "transferred",
            "reason": "client_not_started",
            "released_token_bound": 20,
            "released_cost_bound": 1.0,
            "reserved_token_bound": 10,
            "reserved_cost_bound": 0.5,
        }
    )
    return ModelRouteChainState.model_validate(payload)


allocation_transferred_state = _allocation_transferred_state


def _direct_route_identity(root_id: str) -> OperationIdentity:
    """构造与 direct owner 和初始 route reservation 完全绑定的 v2 身份。"""

    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"test-only-budget-fingerprint-key",
        fingerprint_key_version="test-v1",
        ownership_kind="direct",
        run_id=root_id,
        agent_id="agent-a",
        delegation_claim_id=None,
        usage_kind="model",
        operation_slot=_CHAIN_USAGE_ID,
        semantic_request={"prompt_ref": "route-chain-direct"},
        tree_snapshot_id=f"snapshot:{root_id}",
        agent_sub_snapshot_id=f"snapshot:{root_id}:agent-a",
        provider="fake",
        model="fake-basic",
        price_source_ref="price:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=20,
        trusted_cost_bound=Decimal("1.00"),
        route_chain_digest="a" * 64,
        route_candidate_count=2,
    )


async def _create_route_chain_operation(
    storage: SQLAlchemyStorage,
    *,
    ownership_kind: Literal["direct", "allocation"],
    suffix: str,
    route_chain_state: ModelRouteChainState | None = None,
) -> str:
    """经公开 claim seam 创建带 20/1.00 reservation 的 direct 或 allocation。"""

    root_id = await create_root(storage, suffix=suffix)
    initial = route_chain_state or _allocation_route_state()
    token_reservation = initial.current_reservation.token_bound
    cost_reservation = (
        None
        if initial.current_reservation.cost_bound is None
        else Decimal(str(initial.current_reservation.cost_bound))
    )
    if ownership_kind == "direct":
        async with storage.uow() as uow:
            await uow.shared_budget.claim_direct(
                DirectBudgetClaim(
                    tenant_id="tenant-a",
                    budget_owner_run_id=root_id,
                    usage_call_id=_CHAIN_USAGE_ID,
                    identity=_direct_route_identity(root_id),
                    token_reservation=token_reservation,
                    cost_reservation=cost_reservation,
                    route_chain_state=initial,
                )
            )
            await uow.commit()
        return root_id

    delegation_id, child_id = await create_delegation(
        storage,
        root_id=root_id,
        suffix=suffix,
    )
    async with storage.uow() as uow:
        await uow.session.execute(
            update(AgentRunModel)
            .where(AgentRunModel.id == child_id)
            .values(idempotency_key=f"delegation:{delegation_id}")
        )
        await uow.shared_budget.allocate(
            AllocationBudgetClaim(
                tenant_id="tenant-a",
                budget_owner_run_id=root_id,
                delegation_id=delegation_id,
                usage_call_id=_CHAIN_USAGE_ID,
                identity=_allocation_v2_identity(
                    root_id=root_id,
                    child_id=child_id,
                    delegation_id=delegation_id,
                ),
                token_reservation=token_reservation,
                cost_reservation=cost_reservation,
                route_chain_state=initial,
            )
        )
        await uow.commit()
    return child_id


create_route_chain_operation = _create_route_chain_operation


def _waiting_route_state() -> ModelRouteChainState:
    """构造零影响的首候选审批等待 state，供 repository 查询合同复用。"""

    payload = _allocation_route_state().to_payload()
    payload["active_ordinal"] = None
    payload["waiting_approval_ordinal"] = 1
    payload["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    payload["candidates"][0].update(
        {
            "state": "waiting_approval",
            "reason": "approval_required",
            "approval_request_binding_digest": "9" * 64,
        }
    )
    payload["transitions"] = [
        {
            "sequence": 1,
            "from_ordinal": None,
            "to_ordinal": 1,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
    ]
    return ModelRouteChainState.model_validate(payload)


waiting_route_state = _waiting_route_state


async def _assert_nonterminal_mutation_preserves_reservation(
    storage: SQLAlchemyStorage,
    *,
    run_id: str,
    mutation: Literal["attempt_started", "proof", "delta", "close_unknown"],
) -> None:
    """指定非终态 mutation 必须在仓储写入前拒绝降低当前 reservation。"""

    initial = _allocation_route_state()
    started = _allocation_route_state(started=True)
    before = initial
    if mutation != "attempt_started":
        async with storage.uow() as uow:
            await uow.shared_budget.append_model_route_attempt_started(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=_CHAIN_USAGE_ID,
                state=started,
            )
            await uow.commit()
        before = started
    if mutation == "close_unknown":
        forged = close_route_attempt(
            started,
            candidate_ordinal=1,
            lifecycle_state="unknown",
            response_observed=False,
            request_sent=False,
        ).to_payload()
        method_name = "close_model_route_attempt"
    elif mutation == "proof":
        forged = _allocation_proven_state().to_payload()
        method_name = "append_model_route_not_started_proof"
    elif mutation == "delta":
        forged = mark_route_delta_observed(
            started,
            candidate_ordinal=1,
        ).to_payload()
        method_name = "mark_model_route_delta_observed"
    else:
        forged = started.to_payload()
        method_name = "append_model_route_attempt_started"
    forged["current_reservation"] = {
        "candidate_ordinal": 1,
        "token_bound": 0,
        "cost_bound": None,
    }
    async with storage.uow() as uow:
        with pytest.raises(BudgetOperationConflict):
            method = getattr(uow.shared_budget, method_name)
            await method(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=_CHAIN_USAGE_ID,
                state=ModelRouteChainState.model_validate(forged),
            )
    async with storage.uow() as uow:
        assert (
            await uow.shared_budget.get_model_route_chain_state(
                tenant_id="tenant-a",
                run_id=run_id,
                usage_call_id=_CHAIN_USAGE_ID,
            )
            == before
        )
        direct = await uow.session.scalar(
            select(BudgetOperationClaimModel).where(
                BudgetOperationClaimModel.tenant_id == "tenant-a",
                BudgetOperationClaimModel.usage_call_id == _CHAIN_USAGE_ID,
            )
        )
        allocation = await uow.session.scalar(
            select(DelegationBudgetAllocationModel).where(
                DelegationBudgetAllocationModel.tenant_id == "tenant-a",
                DelegationBudgetAllocationModel.usage_call_id == _CHAIN_USAGE_ID,
            )
        )
        operation = direct or allocation
        operation_impact = (
            None if operation is None else (operation.token_impact, operation.reserved_tokens)
        )
    assert operation_impact == (20, 20)


assert_nonterminal_mutation_preserves_reservation = (
    _assert_nonterminal_mutation_preserves_reservation
)


def _v2_identity() -> OperationIdentity:
    """用公开 constructor 生成 chain v2 身份，ordinal 1 仍承载兼容投影。"""

    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"controlled-failover-fingerprint-key",
        fingerprint_key_version="fingerprint-v1",
        ownership_kind="direct",
        run_id="run-a",
        agent_id="agent-a",
        delegation_claim_id=None,
        usage_kind="model",
        operation_slot="usage-a",
        semantic_request={"prompt": "hello", "route_chain_digest": "a" * 64},
        tree_snapshot_id="tree-a",
        agent_sub_snapshot_id="agent-snapshot-a",
        provider="openai-compatible",
        model="model-a",
        price_source_ref="price-a",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=100,
        trusted_cost_bound=Decimal("0.01"),
        route_chain_digest="a" * 64,
        route_candidate_count=3,
    )


def test_route_chain_uses_budget_operation_v2_without_changing_ordinal_one_projection() -> None:
    """显式 chain 唯一使用 v2，完整链摘要/count 与 ordinal 1 兼容位同时参与 hash。"""

    identity = _v2_identity()

    assert identity.identity_schema_version == "budget-operation-v2"
    assert identity.provider == "openai-compatible"
    assert identity.model == "model-a"
    assert identity.route_chain_digest == "a" * 64
    assert identity.route_candidate_count == 3
    assert OperationIdentity.model_validate(identity.to_payload()) == identity


def test_v1_and_v2_identity_shapes_cannot_be_mixed() -> None:
    """v1 夹带链字段或 v2 缺失摘要/count 都必须在余额读取前关闭失败。"""

    identity = _v2_identity()
    v2_payload = identity.to_payload()

    for missing in ["route_chain_digest", "route_candidate_count"]:
        invalid = dict(v2_payload)
        del invalid[missing]
        with pytest.raises(ValidationError):
            OperationIdentity.model_validate(invalid)

    legacy = dict(v2_payload)
    legacy["identity_schema_version"] = "budget-operation-v1"
    with pytest.raises(ValidationError):
        OperationIdentity.model_validate(legacy)


def test_0017_columns_and_revision_are_declared_without_rewriting_legacy_rows() -> None:
    """两个 owner 表都只新增 nullable JSON state，Alembic 唯一 head 必须是 0017。"""

    claim_column = BudgetOperationClaimModel.__table__.c.route_chain_state_json
    allocation_column = DelegationBudgetAllocationModel.__table__.c.route_chain_state_json

    assert claim_column.nullable is True
    assert allocation_column.nullable is True
    assert get_head_revision() == "0018_model_tool_loop_state"


@pytest.mark.asyncio
async def test_uow_exposes_symmetric_attempt_proof_and_transfer_operations() -> None:
    """测试只走 `uow.shared_budget` 公共 seam，direct/allocation 不暴露私有 mixin。"""

    storage = SQLAlchemyStorage.from_dsn("sqlite+aiosqlite:///:memory:")
    try:
        async with storage.uow() as uow:
            repository = uow.shared_budget
            assert callable(repository.append_model_route_attempt_started)
            assert callable(repository.append_model_route_not_started_proof)
            assert callable(repository.transfer_model_route_reservation)
            assert callable(repository.prove_and_transfer_model_route_reservation)
            assert callable(repository.activate_approved_model_route)
    finally:
        await storage.dispose()
