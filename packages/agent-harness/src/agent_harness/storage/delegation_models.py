"""Delegation claim、预算预约与聚合 ORM。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base


class AgentDelegationModel(Base):
    """显式幂等 claim 与 parent/child delegation 关系。"""

    __tablename__ = "agent_delegations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "parent_run_id",
            "idempotency_key",
            name="uq_agent_delegations_parent_key",
        ),
        UniqueConstraint("child_run_id", name="uq_agent_delegations_child_run"),
        ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_agent_delegations_parent_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["child_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_agent_delegations_child_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('claimed','queued','running','completed','failed',"
            "'released','needs_review')",
            name="ck_agent_delegations_status",
        ),
        CheckConstraint(
            "event_operation_kind = 'delegation'",
            name="ck_agent_delegations_event_kind",
        ),
        CheckConstraint("reserved_event_count > 0", name="ck_agent_delegations_event_count"),
        Index("ix_agent_delegations_parent", "tenant_id", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    child_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    source_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_agent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_intent: Mapped[str] = mapped_column(String(64), nullable=False)
    child_input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    identity_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="claimed", nullable=False)
    error_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    event_operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    event_registry_version: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class DelegationBudgetReservationModel(Base):
    """以 parent 为竞争范围的 durable 最坏情况预算占用。"""

    __tablename__ = "delegation_budget_reservations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_budget_parent_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint("reserved_tokens >= 0", name="ck_delegation_budget_tokens"),
        CheckConstraint(
            "reserved_cost_usd is null or reserved_cost_usd >= 0",
            name="ck_delegation_budget_cost",
        ),
        CheckConstraint(
            "settled_input_tokens is null or settled_input_tokens >= 0",
            name="ck_delegation_budget_settled_input",
        ),
        CheckConstraint(
            "settled_output_tokens is null or settled_output_tokens >= 0",
            name="ck_delegation_budget_settled_output",
        ),
        CheckConstraint(
            "settled_cost_usd is null or settled_cost_usd >= 0",
            name="ck_delegation_budget_settled_cost",
        ),
        CheckConstraint(
            "state != 'settled' or "
            "(settled_input_tokens is not null and settled_output_tokens is not null "
            "and settled_cost_usd is not null)",
            name="ck_delegation_budget_settled_complete",
        ),
        CheckConstraint(
            "state in ('reserved','settled','released','needs_review')",
            name="ck_delegation_budget_state",
        ),
        Index("ix_delegation_budget_parent", "tenant_id", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delegation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_delegations.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    settled_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settled_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settled_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="reserved", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        nullable=False,
    )


class DelegationAggregateModel(Base):
    """由可信 child evidence 生成的可重入 parent aggregation。"""

    __tablename__ = "delegation_aggregates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["parent_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_aggregate_parent_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["child_run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_delegation_aggregate_child_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status in ('complete','incomplete','needs_review')",
            name="ck_delegation_aggregate_status",
        ),
        Index("ix_delegation_aggregates_parent", "tenant_id", "parent_run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delegation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_delegations.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    parent_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    child_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
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
    "AgentDelegationModel",
    "DelegationAggregateModel",
    "DelegationBudgetReservationModel",
]
