"""Eval case、run 与 score 的 ORM 声明。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.orm_base import Base, TimestampMixin


class EvalCaseModel(TimestampMixin, Base):
    """eval case 的草稿/审核状态记录。"""

    __tablename__ = "eval_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    trigger: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
    source_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    artifact_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalRunModel(TimestampMixin, Base):
    """eval run 与源 run / case 的关联记录。"""

    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
    eval_case_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("eval_cases.id"),
        nullable=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_runs.id"), nullable=True
    )
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    score_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    score_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provider_status_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")


class EvalScoreModel(TimestampMixin, Base):
    """一次 eval score 的本地 evidence 与 provider 写回摘要。"""

    __tablename__ = "eval_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    eval_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("eval_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("eval_cases.id"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    metric: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    provider_status_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
    )


__all__ = ["EvalCaseModel", "EvalRunModel", "EvalScoreModel"]
