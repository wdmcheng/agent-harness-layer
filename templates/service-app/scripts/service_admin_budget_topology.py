"""真实 PostgreSQL shared-budget topology 与 cost-disabled 探针。"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from sqlalchemy import select

from agent_harness.config import load_settings
from agent_harness.storage import (
    RunCreate,
    SessionCreate,
    SQLAlchemyStorage,
    storage_dsn_from_settings,
)
from agent_harness.storage.delegation_repositories import (
    DelegationClaimCreate,
)
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    DirectBudgetClaim,
    LedgerCreate,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
)


def storage_dsn() -> str:
    """通过 typed loader 读取 direct env 或受控 secret file 的 storage DSN。"""

    profile = os.environ.get("AGENT_HARNESS_PROFILE", "service")
    return storage_dsn_from_settings(load_settings(profile=profile))


def queue_dsn() -> str:
    value = os.environ.get("AGENT_HARNESS_QUEUE__DSN", "").strip()
    if not value:
        raise RuntimeError("AGENT_HARNESS_QUEUE__DSN is required")
    return value


def _cost_disabled_snapshot(snapshot_id: str, root_run_id: str) -> dict[str, object]:
    """构造真实 PostgreSQL topology proof 使用的冻结 cost-disabled catalog。"""

    agents: dict[str, dict[str, object]] = {
        agent_id: {
            "agent_id": agent_id,
            "descriptor_version": f"{agent_id}-service-smoke-v1",
            "model_policy": {
                "provider": "fake",
                "default_model": "fake-basic",
                "fallback_models": [],
            },
            "target_budget": {
                "max_tokens_per_run": 20 if agent_id == "examples.target" else 100,
                "max_cost_usd_per_run": None,
            },
            "routes": [
                {
                    "usage_kind": usage_kind,
                    "provider": "fake",
                    "model": "fake-basic",
                    "price_source_ref": "catalog:fake",
                    "price_source_version": "v1",
                    "input_token_price_usd": "0",
                    **(
                        {
                            "output_token_price_usd": "0",
                            "soft_max_tokens_per_call": 100,
                        }
                        if usage_kind == "model"
                        else {}
                    ),
                }
                for usage_kind in ("model", "embedding")
            ],
        }
        for agent_id in ("examples.basic", "examples.target")
    }
    return {
        "owner": {
            "agent_id": "examples.basic",
            "root_run_id": root_run_id,
            "delegation_targets": ["examples.target"],
            "max_tokens_per_run": 100,
            "max_cost_usd_per_run": None,
            "cost_enabled": False,
        },
        "registry_version": "service-smoke-registry-v1",
        "config_version": "service-smoke-config-v1",
        "catalog_version": "service-smoke-catalog-v1",
        "snapshot_id": snapshot_id,
        "agents": agents,
    }


def _cost_disabled_identity(
    *,
    tenant_id: str,
    snapshot_id: str,
    run_id: str,
    agent_id: str,
    usage_kind: Literal["model", "embedding"],
    operation_slot: str,
    token_bound: int,
    delegation_id: str | None = None,
) -> OperationIdentity:
    return OperationIdentity.from_semantic_request(
        tenant_id=tenant_id,
        fingerprint_key=b"service-smoke-cost-disabled-proof",
        fingerprint_key_version="service-smoke-key-v1",
        ownership_kind="allocation" if delegation_id is not None else "direct",
        run_id=run_id,
        agent_id=agent_id,
        delegation_claim_id=delegation_id,
        usage_kind=usage_kind,
        operation_slot=operation_slot,
        semantic_request={"slot": operation_slot},
        tree_snapshot_id=snapshot_id,
        agent_sub_snapshot_id=f"{snapshot_id}:{agent_id}",
        provider="fake",
        model="fake-basic",
        price_source_ref="catalog:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=False,
        trusted_token_bound=token_bound,
        trusted_cost_bound=None,
    )


async def assert_budget_topology() -> dict[str, object]:
    """真实 PostgreSQL 证明多 root 隔离、child 不双计与 cost-disabled 结算。"""

    storage = SQLAlchemyStorage.from_dsn(storage_dsn())
    suffix = uuid4().hex
    tenant_id = f"budget-topology-{suffix}"
    session_id = str(uuid4())
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            await uow.sessions.ensure(
                SessionCreate(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id="service-smoke-budget-topology",
                    agent_id="examples.basic",
                )
            )
            roots: list[tuple[str, str, str]] = []
            for label in ("a", "b"):
                root = await uow.runs.create(
                    RunCreate(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        agent_id="examples.basic",
                        trace_id=f"budget-topology-{label}-{suffix}",
                    )
                )
                snapshot_id = f"budget-topology-{label}-{suffix}"
                await uow.shared_budget.create_ledger(
                    LedgerCreate(
                        tenant_id=tenant_id,
                        budget_owner_run_id=root.id,
                        token_limit=100,
                        cost_limit=None,
                        registry_version="service-smoke-registry-v1",
                        config_version="service-smoke-config-v1",
                        catalog_version="service-smoke-catalog-v1",
                        snapshot_id=snapshot_id,
                        snapshot=_cost_disabled_snapshot(snapshot_id, root.id),
                    )
                )
                roots.append((root.id, root.trace_id, snapshot_id))
            await uow.commit()

        async def settle_direct(
            root_id: str,
            snapshot_id: str,
            *,
            usage_kind: Literal["model", "embedding"],
            slot: str,
            bound: int,
            actual: int,
        ) -> None:
            identity = _cost_disabled_identity(
                tenant_id=tenant_id,
                snapshot_id=snapshot_id,
                run_id=root_id,
                agent_id="examples.basic",
                usage_kind=usage_kind,
                operation_slot=slot,
                token_bound=bound,
            )
            async with storage.uow() as uow:
                await uow.shared_budget.claim_direct(
                    DirectBudgetClaim(
                        tenant_id=tenant_id,
                        budget_owner_run_id=root_id,
                        usage_call_id=slot,
                        identity=identity,
                        token_reservation=bound,
                        cost_reservation=None,
                    )
                )
                await uow.shared_budget.mark_direct_started(
                    tenant_id=tenant_id,
                    budget_owner_run_id=root_id,
                    usage_call_id=slot,
                )
                await uow.shared_budget.settle_direct(
                    tenant_id=tenant_id,
                    budget_owner_run_id=root_id,
                    usage_call_id=slot,
                    actual_tokens=actual,
                    actual_cost=None,
                    cost_status="unavailable",
                    result={"provider_called": True, "usage_kind": usage_kind},
                )
                await uow.commit()

        root_a_id, root_a_trace_id, snapshot_a = roots[0]
        root_b_id, _root_b_trace_id, snapshot_b = roots[1]
        await settle_direct(
            root_a_id,
            snapshot_a,
            usage_kind="model",
            slot=f"root-a-model-{suffix}",
            bound=10,
            actual=5,
        )
        await settle_direct(
            root_a_id,
            snapshot_a,
            usage_kind="embedding",
            slot=f"root-a-embedding-{suffix}",
            bound=8,
            actual=4,
        )
        await settle_direct(
            root_b_id,
            snapshot_b,
            usage_kind="model",
            slot=f"root-b-model-{suffix}",
            bound=9,
            actual=3,
        )

        async with storage.uow() as uow:
            claimed = await uow.delegations.claim_and_reserve(
                DelegationClaimCreate(
                    tenant_id=tenant_id,
                    parent_run_id=root_a_id,
                    source_agent_id="examples.basic",
                    target_agent_id="examples.target",
                    idempotency_key=f"topology-delegation-{suffix}",
                    request_hash="e" * 64,
                    budget_intent="inherit_parent",
                    child_input={"smoke": "cost-disabled-allocation"},
                    identity={"user_id": "service-smoke-budget-topology"},
                    trace_id=root_a_trace_id,
                    request_id=f"topology-request-{suffix}",
                    parent_token_limit=100,
                    requested_token_reservation=20,
                    parent_cost_limit=None,
                    requested_cost_reservation=None,
                )
            )
            child = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    agent_id="examples.target",
                    idempotency_key=f"delegation:{claimed.delegation.id}",
                    parent_run_id=root_a_id,
                    trace_id=root_a_trace_id,
                )
            )
            await uow.delegations.attach_child(
                delegation_id=claimed.delegation.id,
                child_run_id=child.id,
            )
            allocation_identity = _cost_disabled_identity(
                tenant_id=tenant_id,
                snapshot_id=snapshot_a,
                run_id=child.id,
                agent_id="examples.target",
                usage_kind="embedding",
                operation_slot=f"child-embedding-{suffix}",
                token_bound=12,
                delegation_id=claimed.delegation.id,
            )
            await uow.shared_budget.allocate(
                AllocationBudgetClaim(
                    tenant_id=tenant_id,
                    budget_owner_run_id=root_a_id,
                    delegation_id=claimed.delegation.id,
                    usage_call_id=f"child-embedding-{suffix}",
                    identity=allocation_identity,
                    token_reservation=12,
                    cost_reservation=None,
                )
            )
            await uow.shared_budget.mark_allocation_started(
                tenant_id=tenant_id,
                budget_owner_run_id=root_a_id,
                delegation_id=claimed.delegation.id,
                usage_call_id=f"child-embedding-{suffix}",
            )
            await uow.shared_budget.settle_allocation(
                tenant_id=tenant_id,
                budget_owner_run_id=root_a_id,
                delegation_id=claimed.delegation.id,
                usage_call_id=f"child-embedding-{suffix}",
                actual_tokens=7,
                actual_cost=None,
                cost_status="unavailable",
                result={"provider_called": True, "usage_kind": "embedding"},
            )
            await uow.shared_budget.settle_delegation(
                delegation_id=claimed.delegation.id,
                actual_tokens=7,
                actual_cost=None,
                cost_status="unavailable",
                needs_review=False,
                result={"outcome": "completed", "token_total": 7},
            )
            await uow.commit()

        invalid_cost_rejected = False
        invalid_slot = f"root-b-invalid-cost-{suffix}"
        invalid_identity = _cost_disabled_identity(
            tenant_id=tenant_id,
            snapshot_id=snapshot_b,
            run_id=root_b_id,
            agent_id="examples.basic",
            usage_kind="model",
            operation_slot=invalid_slot,
            token_bound=4,
        )
        try:
            async with storage.uow() as uow:
                await uow.shared_budget.claim_direct(
                    DirectBudgetClaim(
                        tenant_id=tenant_id,
                        budget_owner_run_id=root_b_id,
                        usage_call_id=invalid_slot,
                        identity=invalid_identity,
                        token_reservation=4,
                        cost_reservation=None,
                    )
                )
                await uow.shared_budget.settle_direct(
                    tenant_id=tenant_id,
                    budget_owner_run_id=root_b_id,
                    usage_call_id=invalid_slot,
                    actual_tokens=2,
                    actual_cost=Decimal("1"),
                    cost_status="unavailable",
                    result={"provider_called": True, "invalid_cost": True},
                )
                await uow.commit()
        except ValueError:
            invalid_cost_rejected = True
        if not invalid_cost_rejected:
            raise RuntimeError("cost-disabled invalid cost/status was accepted")

        async with storage.uow() as uow:
            ledger_a = await uow.shared_budget.get_ledger(tenant_id, root_a_id)
            ledger_b = await uow.shared_budget.get_ledger(tenant_id, root_b_id)
            allocation_rows = list(
                await uow.session.scalars(
                    select(DelegationBudgetAllocationModel).where(
                        DelegationBudgetAllocationModel.delegation_id == claimed.delegation.id
                    )
                )
            )
            top_claim = await uow.session.scalar(
                select(BudgetOperationClaimModel).where(
                    BudgetOperationClaimModel.delegation_id == claimed.delegation.id
                )
            )
            if (
                ledger_a is None
                or ledger_b is None
                or top_claim is None
                or len(allocation_rows) != 1
            ):
                raise RuntimeError("cost-disabled topology evidence is incomplete")
            allocation = allocation_rows[0]
            top_owner = top_claim.budget_owner_run_id
            top_impact = top_claim.token_impact
            allocation_owner = allocation.budget_owner_run_id
            allocation_impact = allocation.token_impact
            allocation_cost_impact = allocation.cost_impact
        if (
            ledger_a.budget_owner_run_id == ledger_b.budget_owner_run_id
            or ledger_a.token_impact != 16
            or ledger_b.token_impact != 3
            or ledger_a.cost_impact != 0
            or ledger_b.cost_impact != 0
            or top_owner != root_a_id
            or top_impact != 7
            or allocation_owner != root_a_id
            or allocation_impact != 7
            or allocation_cost_impact != 0
        ):
            raise RuntimeError("cost-disabled topology did not preserve owner or aggregate")
        return {
            "roots": 2,
            "root_a_token_impact": ledger_a.token_impact,
            "root_b_token_impact": ledger_b.token_impact,
            "root_a_cost_impact": str(ledger_a.cost_impact),
            "root_b_cost_impact": str(ledger_b.cost_impact),
            "delegation_top_impact": top_impact,
            "allocation_impact": allocation_impact,
            "child_double_counted": False,
            "cost_disabled_model_embedding_delegated_child": "settled",
            "invalid_cost_status_rejected": invalid_cost_rejected,
        }
    finally:
        await storage.dispose()


__all__ = ["assert_budget_topology"]
