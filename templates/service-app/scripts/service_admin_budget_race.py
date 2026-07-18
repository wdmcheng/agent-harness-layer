"""真实 PostgreSQL shared-budget direct/delegation 原子竞争探针。"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

from agent_harness.config import load_settings
from agent_harness.storage import (
    RunCreate,
    SessionCreate,
    SQLAlchemyStorage,
    storage_dsn_from_settings,
)
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
)
from agent_harness.storage.shared_budget import (
    BudgetReservationRejected,
    DirectBudgetClaim,
    LedgerCreate,
    OperationIdentity,
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


async def assert_budget_race() -> dict[str, object]:
    """在真实 PostgreSQL 上证明 token/cost 双维竞争只提交一个合法 claim。"""

    storage = SQLAlchemyStorage.from_dsn(storage_dsn())
    suffix = uuid4().hex
    tenant_id = f"budget-race-{suffix}"
    session_id = str(uuid4())
    snapshot_id = f"budget-race-snapshot-{suffix}"
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure(tenant_id)
            await uow.sessions.ensure(
                SessionCreate(
                    session_id=session_id,
                    tenant_id=tenant_id,
                    user_id="service-smoke-budget-race",
                    agent_id="examples.basic",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    agent_id="examples.basic",
                    trace_id=f"budget-race-trace-{suffix}",
                )
            )
            root_run_id = run.id
            await uow.shared_budget.create_ledger(
                LedgerCreate(
                    tenant_id=tenant_id,
                    budget_owner_run_id=run.id,
                    token_limit=100,
                    cost_limit=Decimal("5"),
                    registry_version="service-smoke-registry-v1",
                    config_version="service-smoke-config-v1",
                    catalog_version="service-smoke-catalog-v1",
                    snapshot_id=snapshot_id,
                    snapshot={
                        "owner": {
                            "agent_id": "examples.basic",
                            "root_run_id": run.id,
                            "delegation_targets": ["examples.target"],
                            "max_tokens_per_run": 100,
                            "max_cost_usd_per_run": "5",
                            "cost_enabled": True,
                        },
                        "registry_version": "service-smoke-registry-v1",
                        "config_version": "service-smoke-config-v1",
                        "catalog_version": "service-smoke-catalog-v1",
                        "agents": {
                            "examples.basic": {
                                "agent_id": "examples.basic",
                                "descriptor_version": "service-smoke-v1",
                                "model_policy": {
                                    "provider": "fake",
                                    "default_model": "fake-basic",
                                    "fallback_models": [],
                                },
                                "target_budget": {
                                    "max_tokens_per_run": 100,
                                    "max_cost_usd_per_run": "5",
                                },
                                "routes": [
                                    {
                                        "usage_kind": "model",
                                        "provider": "fake",
                                        "model": "fake-basic",
                                        "price_source_ref": "catalog:fake",
                                        "price_source_version": "v1",
                                        "input_token_price_usd": "0",
                                        "output_token_price_usd": "0",
                                        "soft_max_tokens_per_call": 100,
                                    }
                                ],
                            },
                            "examples.target": {
                                "agent_id": "examples.target",
                                "descriptor_version": "service-smoke-target-v1",
                                "model_policy": {
                                    "provider": "fake",
                                    "default_model": "fake-basic",
                                    "fallback_models": [],
                                },
                                "target_budget": {
                                    "max_tokens_per_run": 100,
                                    "max_cost_usd_per_run": "5",
                                },
                                "routes": [
                                    {
                                        "usage_kind": "model",
                                        "provider": "fake",
                                        "model": "fake-basic",
                                        "price_source_ref": "catalog:fake",
                                        "price_source_version": "v1",
                                        "input_token_price_usd": "0",
                                        "output_token_price_usd": "0",
                                        "soft_max_tokens_per_call": 100,
                                    }
                                ],
                            },
                        },
                    },
                )
            )
            await uow.commit()

        async def compete_direct() -> str:
            slot = "direct"
            identity = OperationIdentity.from_semantic_request(
                tenant_id=tenant_id,
                fingerprint_key=os.urandom(32),
                fingerprint_key_version="service-smoke-key-v1",
                ownership_kind="direct",
                run_id=root_run_id,
                agent_id="examples.basic",
                delegation_claim_id=None,
                usage_kind="model",
                operation_slot=slot,
                semantic_request={"slot": slot},
                tree_snapshot_id=snapshot_id,
                agent_sub_snapshot_id=f"{snapshot_id}:examples.basic",
                provider="fake",
                model="fake-basic",
                price_source_ref="catalog:fake",
                price_source_version="v1",
                cache_key_digest=None,
                cost_enabled=True,
                trusted_token_bound=60,
                trusted_cost_bound=Decimal("3"),
            )
            try:
                async with storage.uow() as uow:
                    await uow.shared_budget.claim_direct(
                        DirectBudgetClaim(
                            tenant_id=tenant_id,
                            budget_owner_run_id=root_run_id,
                            usage_call_id=f"budget-race-{slot}",
                            identity=identity,
                            token_reservation=60,
                            cost_reservation=Decimal("3"),
                        )
                    )
                    await uow.commit()
                return "committed"
            except BudgetReservationRejected:
                return "rejected"

        async def compete_delegation() -> str:
            try:
                async with storage.uow() as uow:
                    await uow.delegations.claim_and_reserve(
                        DelegationClaimCreate(
                            tenant_id=tenant_id,
                            parent_run_id=root_run_id,
                            source_agent_id="examples.basic",
                            target_agent_id="examples.target",
                            idempotency_key=f"budget-race-delegation-{suffix}",
                            request_hash="d" * 64,
                            budget_intent="inherit_parent",
                            child_input={"smoke": "mixed-race"},
                            identity={"user_id": "service-smoke-budget-race"},
                            trace_id=f"budget-race-trace-{suffix}",
                            request_id=f"budget-race-request-{suffix}",
                            parent_token_limit=100,
                            requested_token_reservation=60,
                            parent_cost_limit=5.0,
                            requested_cost_reservation=3.0,
                        )
                    )
                    await uow.commit()
                return "committed"
            except DelegationBudgetExceeded:
                return "rejected"

        outcomes = await asyncio.gather(compete_direct(), compete_delegation())
        async with storage.uow() as uow:
            ledger = await uow.shared_budget.get_ledger(tenant_id, root_run_id)
        if ledger is None:
            raise RuntimeError("budget race ledger is missing")
        if sorted(outcomes) != ["committed", "rejected"]:
            raise RuntimeError("budget race did not converge")
        return {
            "committed": outcomes.count("committed"),
            "rejected": outcomes.count("rejected"),
            "token_limit": ledger.token_limit,
            "token_impact": ledger.token_impact,
            "cost_limit": str(ledger.cost_limit),
            "cost_impact": str(ledger.cost_impact),
            "competition": "direct+delegation",
        }
    finally:
        await storage.dispose()


__all__ = ["assert_budget_race"]
