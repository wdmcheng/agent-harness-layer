"""Harness 人工验收决定 DTO 与仓储，确保每个实验只有一条不可变审阅结论。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.eval_experiment_models import (
    EvalExperimentModel,
    HarnessAcceptanceModel,
)
from agent_harness.storage.eval_experiment_repositories import (
    ExperimentStorageConcurrentConflict,
    ExperimentStorageConflict,
    ExperimentStorageNotFound,
)


class HarnessAcceptanceCreate(HarnessDTO):
    """人工 accepted/rejected review decision 的持久化输入。"""

    tenant_id: str
    experiment_id: str
    decision_request_hash: str
    reviewer_id: str
    reason: str
    decision: Literal["accepted", "rejected"]
    accepted_harness_version: str | None = None
    production_binding: dict[str, Any] | None = None
    policy_decision: dict[str, Any]
    audit_ref: str
    evidence_refs: list[str] = Field(default_factory=list)
    followup_issue_ref: str | None = None

    @model_validator(mode="after")
    def validate_binding(self) -> HarnessAcceptanceCreate:
        """校验接受与拒绝决定的生产绑定字段互斥，防止拒绝结果误发布版本。"""
        if self.decision == "accepted" and (
            self.accepted_harness_version is None or self.production_binding is None
        ):
            raise ValueError("accepted decision requires harness version and production binding")
        if self.decision == "rejected" and (
            self.accepted_harness_version is not None or self.production_binding is not None
        ):
            raise ValueError("rejected decision cannot create a production binding")
        return self


class HarnessAcceptanceRecord(HarnessAcceptanceCreate):
    """每个 experiment 唯一且不可变的人工 decision。"""

    acceptance_id: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class HarnessAcceptanceRepository:
    """同一 experiment 的 decision 只允许一次，完全相同请求可重放。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前工作单元 session；决定创建与实验状态更新由外层统一提交。"""
        self._session = session

    async def create(self, data: HarnessAcceptanceCreate) -> HarnessAcceptanceRecord:
        """创建唯一人工决定；完全相同请求重放，任何差异都报告不可变冲突。

        先在租户范围内读取既有决定，再依赖数据库唯一约束处理并发竞争；并发
        写入失败不会被自动覆盖，调用方必须重新读取获胜的人工审阅事实。
        """
        result = await self._session.scalars(
            select(HarnessAcceptanceModel).where(
                HarnessAcceptanceModel.tenant_id == data.tenant_id,
                HarnessAcceptanceModel.experiment_id == data.experiment_id,
            )
        )
        existing = result.first()
        if existing is not None:
            if not _acceptance_matches(existing, data):
                raise ExperimentStorageConflict(
                    "eval.experiment.decision_conflict",
                    "experiment already has another immutable review decision",
                )
            return _acceptance_record(existing)
        experiment = await self._session.get(EvalExperimentModel, data.experiment_id)
        if experiment is None or experiment.tenant_id != data.tenant_id:
            raise ExperimentStorageNotFound(
                "eval.experiment.not_found", "eval experiment is not visible"
            )
        model = HarnessAcceptanceModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            experiment_id=data.experiment_id,
            decision_request_hash=data.decision_request_hash,
            reviewer_id=data.reviewer_id,
            reason=data.reason,
            decision=data.decision,
            accepted_harness_version=data.accepted_harness_version,
            production_binding_json=data.production_binding,
            policy_decision_json=data.policy_decision,
            audit_ref=data.audit_ref,
            evidence_refs_json=data.evidence_refs,
            followup_issue_ref=data.followup_issue_ref,
        )
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise ExperimentStorageConcurrentConflict(
                "eval.experiment.decision_conflict",
                "concurrent review decision must be reconciled",
            ) from exc
        return _acceptance_record(model)

    async def get_for_experiment(
        self, tenant_id: str, experiment_id: str
    ) -> HarnessAcceptanceRecord | None:
        """读取租户内实验的唯一验收决定；未审阅实验返回空值而非伪造默认结论。"""
        result = await self._session.scalars(
            select(HarnessAcceptanceModel).where(
                HarnessAcceptanceModel.tenant_id == tenant_id,
                HarnessAcceptanceModel.experiment_id == experiment_id,
            )
        )
        model = result.first()
        return None if model is None else _acceptance_record(model)


def _acceptance_record(model: HarnessAcceptanceModel) -> HarnessAcceptanceRecord:
    """将 ORM 决定投影为 DTO，使调用者无需依赖 session 也能读取审计证据。"""
    return HarnessAcceptanceRecord(
        acceptance_id=model.id,
        tenant_id=model.tenant_id,
        experiment_id=model.experiment_id,
        decision_request_hash=model.decision_request_hash,
        reviewer_id=model.reviewer_id,
        reason=model.reason,
        decision=cast(Literal["accepted", "rejected"], model.decision),
        accepted_harness_version=model.accepted_harness_version,
        production_binding=model.production_binding_json,
        policy_decision=model.policy_decision_json,
        audit_ref=model.audit_ref,
        evidence_refs=model.evidence_refs_json,
        followup_issue_ref=model.followup_issue_ref,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _acceptance_matches(model: HarnessAcceptanceModel, data: HarnessAcceptanceCreate) -> bool:
    """比较定义幂等性的不可变人工决策字段，不把生成时间等派生字段纳入比较。"""
    return (
        model.decision_request_hash == data.decision_request_hash
        and model.reviewer_id == data.reviewer_id
        and model.reason == data.reason
        and model.decision == data.decision
        and model.accepted_harness_version == data.accepted_harness_version
        and model.followup_issue_ref == data.followup_issue_ref
    )
