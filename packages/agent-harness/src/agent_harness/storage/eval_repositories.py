"""Eval gate repository DTO 与 SQLAlchemy 实现。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.contracts.dto import HarnessDTO
from agent_harness.storage.models import EvalCaseModel, EvalRunModel, EvalScoreModel
from agent_harness.storage.run_trace_gate import (
    canonical_trace_for_run,
    project_canonical_run_trace,
)


def _empty_provider_status_payloads() -> list[dict[str, object]]:
    return []


class EvalCaseCreate(HarnessDTO):
    """创建 draft eval case 的 repository 输入。"""

    tenant_id: str
    agent_id: str
    name: str
    run_id: str | None = None
    trace_id: str | None = None
    trigger: str = "failed_run"
    dataset: str = "default"
    source_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvalCaseRecord(EvalCaseCreate):
    """已持久化 eval case 的公开记录。"""

    case_id: str
    status: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    review_reason: str | None = None


class EvalRunCreate(HarnessDTO):
    """创建 eval run 摘要记录的 repository 输入。"""

    tenant_id: str
    agent_id: str
    dataset: str = "default"
    status: str = "created"
    eval_case_id: str | None = None
    run_id: str | None = None
    case_count: int = 0
    score_summary: dict[str, Any] = Field(default_factory=dict)
    provider_statuses: list[dict[str, object]] = Field(
        default_factory=_empty_provider_status_payloads
    )


class EvalRunRecord(EvalRunCreate):
    """已持久化 eval run 记录。"""

    eval_run_id: str
    trace_id: str | None = None


class EvalScoreCreate(HarnessDTO):
    """写入单条 eval score 的 repository 输入。"""

    tenant_id: str
    eval_run_id: str
    case_id: str
    metric: str
    value: float
    agent_id: str | None = None
    run_id: str | None = None
    trace_id: str | None = None
    label: str | None = None
    explanation: str | None = None
    provider_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    provider_statuses: list[dict[str, object]] = Field(
        default_factory=_empty_provider_status_payloads
    )


class EvalScoreRecord(EvalScoreCreate):
    """已持久化 eval score 记录。"""

    score_id: str


def _case_record(model: EvalCaseModel) -> EvalCaseRecord:
    return EvalCaseRecord(
        case_id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id or "",
        run_id=model.run_id,
        trace_id=model.trace_id,
        name=model.name,
        status=model.status,
        trigger=model.trigger or "failed_run",
        dataset=model.dataset or "default",
        source_refs=model.source_refs_json or [],
        artifact_refs=model.artifact_refs_json or [],
        payload=model.payload_json,
        metadata=model.metadata_json or {},
        approved_by=model.approved_by,
        approved_at=model.approved_at,
        review_reason=model.review_reason,
    )


def _run_record(model: EvalRunModel) -> EvalRunRecord:
    return EvalRunRecord(
        eval_run_id=model.id,
        tenant_id=model.tenant_id,
        agent_id=model.agent_id or "",
        dataset=model.dataset or "default",
        status=model.status,
        eval_case_id=model.eval_case_id,
        run_id=model.run_id,
        trace_id=model.trace_id,
        case_count=model.case_count,
        score_summary=model.score_summary_json or model.score_json or {},
        provider_statuses=model.provider_status_json or [],
    )


def _score_record(model: EvalScoreModel) -> EvalScoreRecord:
    return EvalScoreRecord(
        score_id=model.id,
        tenant_id=model.tenant_id,
        eval_run_id=model.eval_run_id,
        case_id=model.case_id,
        agent_id=model.agent_id,
        run_id=model.run_id,
        trace_id=model.trace_id,
        metric=model.metric,
        value=model.value,
        label=model.label,
        explanation=model.explanation,
        provider_ref=model.provider_ref,
        metadata=model.metadata_json,
        provider_statuses=model.provider_status_json,
    )


class EvalCaseRepository:
    """draft/approved eval case repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalCaseCreate) -> EvalCaseRecord:
        """创建 draft case；approved 写入必须走 approve。"""

        trace_id = data.trace_id
        if data.run_id is not None:
            trace_id = await project_canonical_run_trace(
                self._session,
                tenant_id=data.tenant_id,
                run_id=data.run_id,
                trace_id=data.trace_id,
            )

        model = EvalCaseModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            trace_id=trace_id,
            name=data.name,
            status="draft",
            trigger=data.trigger,
            dataset=data.dataset,
            source_refs_json=data.source_refs,
            artifact_refs_json=data.artifact_refs,
            payload_json=data.payload,
            metadata_json=data.metadata,
        )
        self._session.add(model)
        await self._session.flush()
        return _case_record(model)

    async def get(self, case_id: str) -> EvalCaseRecord | None:
        model = await self._session.get(EvalCaseModel, case_id)
        return None if model is None else _case_record(model)

    async def list(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        dataset: str | None = None,
        agent_id: str | None = None,
    ) -> list[EvalCaseRecord]:
        """按用户可见边界列出 case，不暴露 ORM row。"""

        conditions = [EvalCaseModel.tenant_id == tenant_id]
        if status is not None:
            conditions.append(EvalCaseModel.status == status)
        if dataset is not None:
            conditions.append(EvalCaseModel.dataset == dataset)
        if agent_id is not None:
            conditions.append(EvalCaseModel.agent_id == agent_id)
        result = await self._session.scalars(
            select(EvalCaseModel).where(*conditions).order_by(EvalCaseModel.created_at.asc())
        )
        return [_case_record(model) for model in result.all()]

    async def approve(
        self,
        *,
        case_id: str,
        tenant_id: str,
        approved_by: str,
        reason: str,
        dataset: str = "default",
    ) -> EvalCaseRecord:
        """人工审核把 draft 转成 approved；自动 detector 不调用这个入口。"""

        model = await self._session.get(EvalCaseModel, case_id)
        if model is None or model.tenant_id != tenant_id:
            raise LookupError(f"eval case not found: {case_id}")
        if model.status != "draft":
            raise RuntimeError(f"eval case is already {model.status}: {case_id}")
        model.status = "approved"
        model.dataset = dataset
        model.approved_by = approved_by
        model.approved_at = datetime.now(tz=UTC)
        model.review_reason = reason
        model.metadata_json = {
            **(model.metadata_json or {}),
            "review": {"reason": reason, "reviewer": approved_by},
        }
        await self._session.flush()
        return _case_record(model)


class EvalRunRepository:
    """eval run summary repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalRunCreate) -> EvalRunRecord:
        trace_id = None
        if data.run_id is not None:
            trace_id = await canonical_trace_for_run(
                self._session,
                tenant_id=data.tenant_id,
                run_id=data.run_id,
            )
        model = EvalRunModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            dataset=data.dataset,
            eval_case_id=data.eval_case_id,
            run_id=data.run_id,
            trace_id=trace_id,
            score_json=data.score_summary,
            score_summary_json=data.score_summary,
            provider_status_json=data.provider_statuses,
            case_count=data.case_count,
            status=data.status,
        )
        self._session.add(model)
        await self._session.flush()
        return _run_record(model)

    async def get(self, eval_run_id: str) -> EvalRunRecord | None:
        model = await self._session.get(EvalRunModel, eval_run_id)
        return None if model is None else _run_record(model)

    async def update_score_evidence(
        self,
        *,
        eval_run_id: str,
        score_summary: dict[str, object],
        provider_statuses: list[dict[str, object]],
    ) -> EvalRunRecord:
        model = await self._session.get(EvalRunModel, eval_run_id)
        if model is None:
            raise LookupError(f"eval run not found: {eval_run_id}")
        model.score_json = score_summary
        model.score_summary_json = score_summary
        model.provider_status_json = provider_statuses
        await self._session.flush()
        return _run_record(model)


class EvalScoreRepository:
    """eval score evidence repository。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: EvalScoreCreate) -> EvalScoreRecord:
        trace_id = data.trace_id
        if data.run_id is not None:
            trace_id = await project_canonical_run_trace(
                self._session,
                tenant_id=data.tenant_id,
                run_id=data.run_id,
                trace_id=data.trace_id,
            )
        model = EvalScoreModel(
            id=str(uuid4()),
            tenant_id=data.tenant_id,
            eval_run_id=data.eval_run_id,
            case_id=data.case_id,
            agent_id=data.agent_id,
            run_id=data.run_id,
            trace_id=trace_id,
            metric=data.metric,
            value=data.value,
            label=data.label,
            explanation=data.explanation,
            provider_ref=data.provider_ref,
            metadata_json=data.metadata,
            provider_status_json=data.provider_statuses,
        )
        self._session.add(model)
        await self._session.flush()
        return _score_record(model)

    async def list_for_run(self, eval_run_id: str) -> list[EvalScoreRecord]:
        result = await self._session.scalars(
            select(EvalScoreModel)
            .where(EvalScoreModel.eval_run_id == eval_run_id)
            .order_by(EvalScoreModel.created_at.asc())
        )
        return [_score_record(model) for model in result.all()]
