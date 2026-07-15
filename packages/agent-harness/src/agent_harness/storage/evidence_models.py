"""Run event 容量与 durable evidence outbox 的 ORM 声明。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base


class RunEvidenceOutboxModel(Base):
    """usage/approval/terminal evidence 的稳定 event-id settlement 状态。"""

    __tablename__ = "run_evidence_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "tenant_id"],
            ["agent_runs.id", "agent_runs.tenant_id"],
            name="fk_run_evidence_outbox_run_tenant",
        ),
        UniqueConstraint(
            "tenant_id",
            "usage_call_id",
            name="uq_run_evidence_outbox_tenant_usage_call",
        ),
        UniqueConstraint("event_id", name="uq_run_evidence_outbox_event_id"),
        UniqueConstraint(
            "group_id",
            "sequence_in_group",
            name="uq_run_evidence_outbox_group_sequence",
        ),
        CheckConstraint(
            "(group_id IS NULL AND sequence_in_group IS NULL) OR "
            "(group_id IS NOT NULL AND sequence_in_group > 0)",
            name="ck_run_evidence_outbox_group_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    usage_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operation_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    group_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    sequence_in_group: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["RunEvidenceOutboxModel"]
