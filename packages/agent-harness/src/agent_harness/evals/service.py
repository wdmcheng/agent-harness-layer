"""Eval Gate application service。"""

from __future__ import annotations

from pathlib import Path

from agent_harness.audit import AuditService
from agent_harness.contracts.dto import HarnessDTO
from agent_harness.evals.cases import EvalCaseFactory, EvalDraftDetector, EvalTraceSource
from agent_harness.evals.review_queue import ReviewDatasetAdapter
from agent_harness.evals.score_sink import ScoreSink
from agent_harness.identity import IdentityContext
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import (
    AuditLogCreate,
    EvalCaseRecord,
    EvalRunCreate,
    EvalRunRecord,
    EvalScoreCreate,
    EvalScoreRecord,
    SQLAlchemyStorage,
)


class EvalCaseApprovalResult(HarnessDTO):
    """人工审核完成后的 case 与审计 evidence 引用。"""

    case: EvalCaseRecord
    audit_ref: str


class EvalService:
    """draft、approval、runner 和 score repository 的业务入口。"""

    def __init__(
        self,
        *,
        storage: SQLAlchemyStorage,
        factory: EvalCaseFactory,
        score_sink: ScoreSink,
        drafts_dir: Path,
        approved_dir: Path,
        audit: AuditService | None = None,
    ) -> None:
        """装配 case 工厂、评审文件队列、评分 sink 和可选审计服务。"""

        self._storage = storage
        self._factory = factory
        self._detector = EvalDraftDetector(factory=factory)
        self._score_sink = score_sink
        self.dataset = ReviewDatasetAdapter(drafts_dir=drafts_dir, approved_dir=approved_dir)
        self.audit = audit

    async def draft_from_trace(
        self,
        source: EvalTraceSource,
        *,
        dataset: str = "default",
        score_threshold: float | None = None,
    ) -> EvalCaseRecord:
        """从 trace source 创建 draft case，并同步写入 draft review queue。"""

        case_create = self._detector.detect(
            source,
            dataset=dataset,
            score_threshold=score_threshold,
        )
        if case_create is None:
            raise ValueError("eval signal did not cross a draft detector threshold")
        async with self._storage.uow() as uow:
            await uow.tenants.ensure(case_create.tenant_id)
            case = await uow.eval_cases.create(case_create)
            await uow.commit()
        self.dataset.write_draft(case)
        return case

    async def approve_case(
        self,
        *,
        actor: IdentityContext,
        case_id: str,
        reason: str,
        dataset: str = "default",
    ) -> EvalCaseApprovalResult:
        """人工审核 draft；approved dataset 写入成功后才提交 DB/audit。"""

        approved_path: Path | None = None
        try:
            async with self._storage.uow() as uow:
                case = await uow.eval_cases.approve(
                    case_id=case_id,
                    tenant_id=actor.tenant_id,
                    approved_by=actor.user_id,
                    reason=reason,
                    dataset=dataset,
                )
                audit_record = await uow.audit_logs.create(
                    AuditLogCreate(
                        tenant_id=actor.tenant_id,
                        actor_user_id=actor.user_id,
                        action="eval.case.approved",
                        resource=f"eval_case:{case.case_id}",
                        payload=redact_secrets(
                            {
                                "case_id": case.case_id,
                                "agent_id": case.agent_id,
                                "run_id": case.run_id,
                                "trace_id": case.trace_id,
                                "dataset": case.dataset,
                                "reason": reason,
                                "result": "approved",
                            }
                        ),
                    )
                )
                approved_path = self.dataset.write_approved(case)
                await uow.commit()
        except Exception:
            if approved_path is not None:
                self.dataset.remove_approved(case_id)
            raise
        self.dataset.remove_draft(case.case_id)
        return EvalCaseApprovalResult(case=case, audit_ref=audit_record.id)

    async def list_cases(
        self,
        *,
        tenant_id: str,
        status: str | None = None,
        dataset: str | None = None,
        agent_id: str | None = None,
    ) -> list[EvalCaseRecord]:
        """按租户和可选状态、数据集、agent 条件列出 case，不跨租户聚合。"""

        async with self._storage.uow() as uow:
            return await uow.eval_cases.list(
                tenant_id=tenant_id,
                status=status,
                dataset=dataset,
                agent_id=agent_id,
            )

    async def create_eval_run(self, data: EvalRunCreate) -> EvalRunRecord:
        """确保租户存在后创建评测运行，并在同一 UoW 内提交。"""

        async with self._storage.uow() as uow:
            await uow.tenants.ensure(data.tenant_id)
            run = await uow.eval_runs.create(data)
            await uow.commit()
            return run

    async def create_score(self, data: EvalScoreCreate) -> EvalScoreRecord:
        """持久化单项评分；事务边界由服务层统一以便调用方获得已提交记录。"""

        async with self._storage.uow() as uow:
            score = await uow.eval_scores.create(data)
            await uow.commit()
            return score

    async def get_eval_run(self, eval_run_id: str) -> EvalRunRecord:
        """读取评测运行，缺失时使用领域无关的查找异常交给路由映射。"""

        async with self._storage.uow() as uow:
            run = await uow.eval_runs.get(eval_run_id)
        if run is None:
            raise LookupError(f"eval run not found: {eval_run_id}")
        return run

    async def update_eval_run_evidence(
        self,
        *,
        eval_run_id: str,
        score_summary: dict[str, object],
        provider_statuses: list[dict[str, object]],
    ) -> EvalRunRecord:
        """原子更新评分汇总与 provider 状态证据，供实验比较与审计读取。"""

        async with self._storage.uow() as uow:
            run = await uow.eval_runs.update_score_evidence(
                eval_run_id=eval_run_id,
                score_summary=score_summary,
                provider_statuses=provider_statuses,
            )
            await uow.commit()
            return run

    async def list_scores(self, eval_run_id: str) -> list[EvalScoreRecord]:
        """按评测运行读取全部持久化评分，不触发重新评分。"""

        async with self._storage.uow() as uow:
            return await uow.eval_scores.list_for_run(eval_run_id)

    @property
    def score_sink(self) -> ScoreSink:
        """暴露已装配的评分接收器，供受控运行路径写入评分结果。"""

        return self._score_sink
