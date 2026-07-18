"""0016 shared parent budget ledger、top-level claim 与 child allocation ORM。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base


class ParentBudgetLedgerModel(Base):
    """以非空 execution-tree root 为唯一竞争范围的冻结账本。"""

    __tablename__ = "parent_budget_ledgers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["budget_owner_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_parent_budget_ledger_owner_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "snapshot_id", name="uq_parent_budget_snapshot"),
        CheckConstraint("token_limit >= 0", name="ck_parent_budget_token_limit"),
        CheckConstraint("token_impact >= 0", name="ck_parent_budget_token_impact"),
        CheckConstraint(
            "(cost_enabled = false and cost_limit is null and cost_impact = 0) or "
            "(cost_enabled = true and cost_limit is not null)",
            name="ck_parent_budget_cost_mode",
        ),
        CheckConstraint("cost_impact >= 0", name="ck_parent_budget_cost_impact"),
        CheckConstraint(
            "state in ('active','needs_review','terminal')", name="ck_parent_budget_state"
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    budget_owner_run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_limit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    cost_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    token_impact: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_impact: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), default=Decimal("0"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    registry_version: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(255), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class BudgetOperationClaimModel(Base):
    """Direct 与 delegation 共用 parent impact 的 top-level operation detail。"""

    __tablename__ = "budget_operation_claims"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "budget_owner_run_id"],
            ["parent_budget_ledgers.tenant_id", "parent_budget_ledgers.budget_owner_run_id"],
            name="fk_budget_claim_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_budget_claim_run_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "budget_owner_run_id",
            "usage_call_id",
            name="uq_budget_claim_direct_usage",
        ),
        UniqueConstraint("delegation_id", name="uq_budget_claim_delegation"),
        CheckConstraint(
            "(operation_kind = 'direct' and usage_call_id is not null "
            "and delegation_id is null) or "
            "(operation_kind = 'delegation' and usage_call_id is null "
            "and delegation_id is not null)",
            name="ck_budget_claim_kind_key",
        ),
        CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_budget_claim_state",
        ),
        CheckConstraint(
            "side_effect_state in ('not_started','started','result_committed')",
            name="ck_budget_claim_side_effect",
        ),
        CheckConstraint(
            "reserved_tokens >= 0 and token_impact >= 0", name="ck_budget_claim_tokens"
        ),
        CheckConstraint(
            "reserved_cost is null or reserved_cost >= 0",
            name="ck_budget_claim_reserved_cost",
        ),
        CheckConstraint("cost_impact >= 0", name="ck_budget_claim_cost_impact"),
        Index("ix_budget_claim_owner_state", "tenant_id", "budget_owner_run_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_owner_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    usage_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delegation_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=True
    )
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    usage_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    identity_schema_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    token_impact: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_impact: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="reserved", nullable=False)
    side_effect_state: Mapped[str] = mapped_column(
        String(24), default="not_started", nullable=False
    )
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    backfill_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class DelegationBudgetAllocationModel(Base):
    """Child usage 在既有 delegation ceiling 内的唯一 allocation。"""

    __tablename__ = "delegation_budget_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "budget_owner_run_id"],
            ["parent_budget_ledgers.tenant_id", "parent_budget_ledgers.budget_owner_run_id"],
            name="fk_budget_allocation_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_budget_allocation_run_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id",
            "budget_owner_run_id",
            "delegation_id",
            "usage_call_id",
            name="uq_budget_allocation_usage",
        ),
        CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_budget_allocation_state",
        ),
        CheckConstraint(
            "side_effect_state in ('not_started','started','result_committed')",
            name="ck_budget_allocation_side_effect",
        ),
        CheckConstraint(
            "reserved_tokens is null or reserved_tokens >= 0",
            name="ck_budget_allocation_reserved_tokens",
        ),
        CheckConstraint(
            "token_impact >= 0 and cost_impact >= 0", name="ck_budget_allocation_impact"
        ),
        Index("ix_budget_allocation_delegation", "delegation_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_owner_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    delegation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_delegations.id", ondelete="RESTRICT"), nullable=False
    )
    usage_call_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    usage_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    identity_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reserved_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    token_impact: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_impact: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    side_effect_state: Mapped[str] = mapped_column(String(24), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    backfill_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


__all__ = [
    "BudgetOperationClaimModel",
    "DelegationBudgetAllocationModel",
    "ParentBudgetLedgerModel",
]
