"""Phase 12.5 eval split、experiment 与人工 decision ORM。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    JSON,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from agent_harness.storage.models import Base, TimestampMixin


class EvalDatasetSplitModel(TimestampMixin, Base):
    """可复现 dataset split 的最小 membership 与 evidence 摘要。"""

    __tablename__ = "eval_dataset_splits"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_eval_dataset_splits_tenant_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False)
    optimization_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    holdout_ratio: Mapped[float] = mapped_column(Float, nullable=False)
    regression_policy_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    case_tags_json: Mapped[dict[str, list[str]]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    optimization_case_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    holdout_case_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    regression_case_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    optimization_case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    holdout_case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    regression_case_count: Mapped[int] = mapped_column(nullable=False, default=0)
    tag_distribution_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    rejected_counts_json: Mapped[dict[str, int]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class EvalExperimentModel(TimestampMixin, Base):
    """同一 split 上 baseline/candidate 实验的本地真相源。"""

    __tablename__ = "eval_experiments"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_eval_experiments_tenant_idempotency",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_eval_experiments_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "split_id"],
            ["eval_dataset_splits.tenant_id", "eval_dataset_splits.id"],
            name="fk_eval_experiments_tenant_split",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    dataset: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    split_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evaluator_profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metric_versions_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    baseline_harness_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    candidate_harness_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    baseline_run_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    candidate_run_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    score_summaries_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    comparison_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    local_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    provider_status_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class HarnessAcceptanceModel(TimestampMixin, Base):
    """每个 experiment 唯一且不可变的人工 review decision。"""

    __tablename__ = "harness_acceptance_records"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "experiment_id",
            name="uq_harness_acceptance_tenant_experiment",
        ),
        UniqueConstraint("experiment_id", name="uq_harness_acceptance_experiment"),
        ForeignKeyConstraint(
            ["tenant_id", "experiment_id"],
            ["eval_experiments.tenant_id", "eval_experiments.id"],
            name="fk_harness_acceptance_tenant_experiment",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id"), index=True)
    experiment_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    decision_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    accepted_harness_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    production_binding_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    policy_decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    audit_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_refs_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    followup_issue_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
