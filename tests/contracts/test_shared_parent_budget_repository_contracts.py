"""0016 shared parent budget ledger repository 合同。"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations
from agent_harness.storage.delegation_models import AgentDelegationModel
from agent_harness.storage.delegation_repositories import (
    DelegationBudgetExceeded,
    DelegationClaimCreate,
    DelegationStorageConflict,
)
from agent_harness.storage.shared_budget import (
    AllocationBudgetClaim,
    BudgetOperationConflict,
    BudgetReservationRejected,
    DirectBudgetClaim,
    LedgerCreate,
    OperationIdentity,
)
from agent_harness.storage.shared_budget_models import (
    BudgetOperationClaimModel,
    DelegationBudgetAllocationModel,
    ParentBudgetLedgerModel,
)


def sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def identity(*, run_id: str, fingerprint: str = "request-a") -> OperationIdentity:
    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"test-only-budget-fingerprint-key",
        fingerprint_key_version="test-v1",
        ownership_kind="direct",
        run_id=run_id,
        agent_id="agent-a",
        delegation_claim_id=None,
        usage_kind="model",
        operation_slot="turn:1:model",
        semantic_request={"prompt_ref": fingerprint},
        tree_snapshot_id=f"snapshot:{run_id}",
        agent_sub_snapshot_id=f"snapshot:{run_id}:agent-a",
        provider="fake",
        model="fake-basic",
        price_source_ref="price:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=60,
        trusted_cost_bound=Decimal("4.00"),
    )


def allocation_identity(
    *,
    root_id: str,
    child_id: str,
    delegation_id: str,
    fingerprint: str = "child-request-a",
    token_bound: int = 20,
) -> OperationIdentity:
    return OperationIdentity.from_semantic_request(
        tenant_id="tenant-a",
        fingerprint_key=b"test-only-budget-fingerprint-key",
        fingerprint_key_version="test-v1",
        ownership_kind="allocation",
        run_id=child_id,
        agent_id="agent-b",
        delegation_claim_id=delegation_id,
        usage_kind="model",
        operation_slot="turn:1:child-model",
        semantic_request={"prompt_ref": fingerprint},
        tree_snapshot_id=f"snapshot:{root_id}",
        agent_sub_snapshot_id=f"snapshot:{root_id}:agent-b",
        provider="fake",
        model="fake-basic",
        price_source_ref="price:fake",
        price_source_version="v1",
        cache_key_digest=None,
        cost_enabled=True,
        trusted_token_bound=token_bound,
        trusted_cost_bound=Decimal("1.00") if token_bound else Decimal("0"),
    )


def corrupt_tree_catalog(snapshot: dict[str, object], case: str) -> dict[str, object]:
    """生成首次写入必须拒绝的 target catalog 反例。"""

    corrupted = deepcopy(snapshot)
    raw_agents = corrupted["agents"]
    assert isinstance(raw_agents, dict)
    agents = cast(dict[str, object], raw_agents)
    if case == "target-missing":
        agents.pop("agent-b")
        return corrupted
    raw_target = agents["agent-b"]
    assert isinstance(raw_target, dict)
    target = cast(dict[str, object], raw_target)
    if case == "agent-mismatch":
        target["agent_id"] = "agent-wrong"
    elif case == "budget-over-owner":
        raw_budget = target["target_budget"]
        assert isinstance(raw_budget, dict)
        budget = cast(dict[str, object], raw_budget)
        budget["max_tokens_per_run"] = 101
    elif case == "route-price-missing":
        raw_routes = target["routes"]
        assert isinstance(raw_routes, list) and isinstance(raw_routes[0], dict)
        routes = cast(list[dict[str, object]], raw_routes)
        routes[0].pop("price_source_ref")
    elif case == "fallback-route-missing":
        raw_policy = target["model_policy"]
        assert isinstance(raw_policy, dict)
        policy = cast(dict[str, object], raw_policy)
        policy["fallback_models"] = ["fake-fallback"]
    else:  # pragma: no cover - 参数表封闭
        raise AssertionError(case)
    return corrupted


async def create_root(
    storage: SQLAlchemyStorage,
    *,
    suffix: str,
    cost_limit: Decimal | None = Decimal("10.00"),
    agent_a_token_limit: int = 100,
    agent_b_token_limit: int = 100,
    agent_b_cost_limit: Decimal | None = None,
) -> str:
    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        session = await uow.sessions.ensure(
            SessionCreate(
                session_id=f"session-{suffix}",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=session.id,
                agent_id="agent-a",
                trace_id=f"trace-{suffix}",
            )
        )
        await uow.shared_budget.create_ledger(
            LedgerCreate(
                tenant_id="tenant-a",
                budget_owner_run_id=run.id,
                token_limit=100,
                cost_limit=cost_limit,
                registry_version="registry-v1",
                config_version="config-v1",
                catalog_version="catalog-v1",
                snapshot_id=f"snapshot:{run.id}",
                snapshot={
                    "owner": {
                        "agent_id": "agent-a",
                        "root_run_id": run.id,
                        "delegation_targets": ["agent-b"],
                        "max_tokens_per_run": 100,
                        "max_cost_usd_per_run": (None if cost_limit is None else str(cost_limit)),
                        "cost_enabled": cost_limit is not None,
                    },
                    "registry_version": "registry-v1",
                    "config_version": "config-v1",
                    "catalog_version": "catalog-v1",
                    "agents": {
                        "agent-a": {
                            "agent_id": "agent-a",
                            "descriptor_version": "agent-a-v1",
                            "model_policy": {
                                "provider": "fake",
                                "default_model": "fake-basic",
                                "fallback_models": [],
                            },
                            "target_budget": {
                                "max_tokens_per_run": agent_a_token_limit,
                                "max_cost_usd_per_run": (
                                    None if cost_limit is None else str(cost_limit)
                                ),
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "price:fake",
                                    "price_source_version": "v1",
                                    "input_token_price_usd": "0",
                                    "output_token_price_usd": "0",
                                    "soft_max_tokens_per_call": 100,
                                }
                            ],
                        },
                        "agent-b": {
                            "agent_id": "agent-b",
                            "descriptor_version": "agent-b-v1",
                            "model_policy": {
                                "provider": "fake",
                                "default_model": "fake-basic",
                                "fallback_models": [],
                            },
                            "target_budget": {
                                "max_tokens_per_run": agent_b_token_limit,
                                "max_cost_usd_per_run": (
                                    None
                                    if cost_limit is None
                                    else str(
                                        cost_limit
                                        if agent_b_cost_limit is None
                                        else agent_b_cost_limit
                                    )
                                ),
                            },
                            "routes": [
                                {
                                    "usage_kind": "model",
                                    "provider": "fake",
                                    "model": "fake-basic",
                                    "price_source_ref": "price:fake",
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
        return run.id


async def create_delegation(
    storage: SQLAlchemyStorage,
    *,
    root_id: str,
    suffix: str,
    token_reservation: int = 60,
    cost_reservation: Decimal = Decimal("4.00"),
    parent_suffix: str | None = None,
) -> tuple[str, str]:
    execution_suffix = suffix if parent_suffix is None else parent_suffix
    async with storage.uow() as uow:
        child = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id=f"session-{execution_suffix}",
                agent_id="agent-b",
                parent_run_id=root_id,
                trace_id=f"trace-{execution_suffix}",
            )
        )
        delegation_id = str(uuid4())
        uow.session.add(
            AgentDelegationModel(
                id=delegation_id,
                tenant_id="tenant-a",
                parent_run_id=root_id,
                child_run_id=child.id,
                source_agent_id="agent-a",
                target_agent_id="agent-b",
                idempotency_key=f"delegation-{suffix}",
                request_hash="d" * 64,
                budget_intent="inherit_parent",
                child_input_json={"query": "safe"},
                identity_json={"user_id": "user-a"},
                trace_id=f"trace-{execution_suffix}",
                request_id=f"request-{suffix}",
                status="running",
                error_json=None,
                event_operation_kind="delegation",
                event_registry_version="v1",
                reserved_event_count=4,
            )
        )
        await uow.session.flush()
        await uow.shared_budget.reserve_delegation(
            tenant_id="tenant-a",
            budget_owner_run_id=root_id,
            delegation_id=delegation_id,
            request_hash="d" * 64,
            token_reservation=token_reservation,
            cost_reservation=cost_reservation,
        )
        await uow.commit()
        return delegation_id, child.id


__all__ = [
    "AgentDelegationModel",
    "AllocationBudgetClaim",
    "BudgetOperationClaimModel",
    "BudgetOperationConflict",
    "BudgetReservationRejected",
    "Decimal",
    "DelegationBudgetAllocationModel",
    "DelegationBudgetExceeded",
    "DelegationClaimCreate",
    "DelegationStorageConflict",
    "DirectBudgetClaim",
    "LedgerCreate",
    "OperationIdentity",
    "ParentBudgetLedgerModel",
    "Path",
    "RunCreate",
    "SQLAlchemyStorage",
    "SessionCreate",
    "allocation_identity",
    "asyncio",
    "cast",
    "corrupt_tree_catalog",
    "create_delegation",
    "create_root",
    "deepcopy",
    "hashlib",
    "identity",
    "json",
    "pytest",
    "run_migrations",
    "select",
    "sqlite_dsn",
    "uuid4",
]
