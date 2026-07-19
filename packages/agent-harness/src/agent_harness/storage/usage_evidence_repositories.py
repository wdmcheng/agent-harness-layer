"""Usage evidence claim、结果持久化与发布结算 repository mixin。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent_harness.storage.event_capacity_repositories import (
    EventCapacityRepository,
    EvidenceOperationKind,
    operation_event_capacity,
)
from agent_harness.storage.models import (
    AgentRunModel,
    RunEventCapacityModel,
    RunEvidenceOutboxModel,
)


def _normalize_started_evidence(
    started_evidence: Mapping[str, object],
    *,
    tenant_id: str,
    run_id: str,
    operation_kind: str,
) -> dict[str, Any]:
    """在 claim 事务内冻结 provider/model 与调用身份，供 sink 防伪。"""

    # 局部 import 避免 storage -> models -> events -> storage 的模块环。
    from agent_harness.models.usage import ModelUsageEvidence

    evidence = ModelUsageEvidence.model_validate(started_evidence)
    expected_usage_kind = "model" if operation_kind == "model_usage" else "embedding"
    if (
        evidence.usage_kind != expected_usage_kind
        or evidence.tenant_id != tenant_id
        or evidence.run_id != run_id
    ):
        raise ValueError("usage started evidence does not match persisted settlement")
    return evidence.to_payload()


def _difference_paths(
    persisted: object,
    replayed: object,
    *,
    prefix: str = "",
) -> list[str]:
    """只返回 identity 差异字段路径；诊断不得复制可能含输入的字段值。"""

    if isinstance(persisted, Mapping) and isinstance(replayed, Mapping):
        persisted_mapping = cast(Mapping[object, object], persisted)
        replayed_mapping = cast(Mapping[object, object], replayed)
        paths: list[str] = []
        keys: list[object] = sorted(
            set(persisted_mapping) | set(replayed_mapping),
            key=str,
        )
        for key in keys:
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in persisted_mapping or key not in replayed_mapping:
                paths.append(path)
                continue
            paths.extend(
                _difference_paths(
                    persisted_mapping[key],
                    replayed_mapping[key],
                    prefix=path,
                )
            )
        return paths
    return [] if persisted == replayed else [prefix or "root"]


@dataclass(frozen=True)
class UsageSettlementClaim:
    """一次 usage claim 的持久化处置，调用方据此决定是否允许副作用。"""

    created: bool
    state: str
    operation_kind: str
    result_json: dict[str, Any] | None
    error_code: str | None


class UsageEvidenceRepositoryMixin:
    """复用同一 UoW session 的 usage settlement 状态机。"""

    _session: AsyncSession

    async def replay_usage(
        self,
        *,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        request_id: str | None,
        trace_id: str,
        usage_call_id: str,
        event_id: str,
        operation_kind: EvidenceOperationKind,
    ) -> UsageSettlementClaim | None:
        """不读取当前 run/capacity，只验证 durable outbox 自身及 execution 绑定。"""

        existing = await self._session.scalar(
            select(RunEvidenceOutboxModel).where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
        )
        if existing is None:
            return None
        persisted_started = (
            existing.result_json.get("started")
            if isinstance(existing.result_json, Mapping)
            else None
        )
        if not isinstance(persisted_started, Mapping):
            raise ValueError("usage settlement is missing its durable started identity")
        normalized_started = _normalize_started_evidence(
            cast(Mapping[str, object], persisted_started),
            tenant_id=tenant_id,
            run_id=run_id,
            operation_kind=operation_kind.value,
        )
        if (
            normalized_started["agent_id"] != agent_id
            or normalized_started.get("request_id") != request_id
            or normalized_started["trace_id"] != trace_id
        ):
            raise ValueError("usage replay does not match execution identity")
        self._validate_usage_binding(
            existing,
            run_id=run_id,
            event_id=event_id,
            operation_kind=operation_kind.value,
            reserved_event_count=operation_event_capacity(operation_kind),
            started_evidence=normalized_started,
        )
        return UsageSettlementClaim(
            created=False,
            state=existing.state,
            operation_kind=existing.operation_kind,
            result_json=existing.result_json,
            error_code=existing.error_code,
        )

    async def claim_usage(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        event_id: str,
        operation_kind: EvidenceOperationKind,
        started_evidence: Mapping[str, object],
    ) -> UsageSettlementClaim:
        """原子竞争唯一 settlement；只有胜者才预约容量并允许外部副作用。"""

        if operation_kind not in {
            EvidenceOperationKind.MODEL_USAGE,
            EvidenceOperationKind.EMBEDDING_USAGE,
        }:
            raise ValueError("invalid usage operation kind")
        if not usage_call_id:
            raise ValueError("usage call id must not be empty")
        run = await self._session.scalar(
            select(AgentRunModel).where(AgentRunModel.id == run_id).with_for_update()
        )
        if run is None:
            raise LookupError(f"usage run is not persisted: {run_id}")
        if run.tenant_id != tenant_id:
            raise ValueError("usage tenant does not own run")
        if run.status in {"completed", "failed", "cancelled"}:
            raise RuntimeError("terminal run does not accept new usage settlement")
        capacity_tenant = await self._session.scalar(
            select(RunEventCapacityModel.tenant_id).where(RunEventCapacityModel.run_id == run_id)
        )
        if capacity_tenant is None:
            raise LookupError(f"event capacity is not initialized: {run_id}")
        if capacity_tenant != tenant_id:
            raise ValueError("usage tenant does not own run")
        reserved_event_count = operation_event_capacity(operation_kind)
        normalized_started = _normalize_started_evidence(
            started_evidence,
            tenant_id=tenant_id,
            run_id=run_id,
            operation_kind=operation_kind.value,
        )
        persisted_request_id: object = None
        request_id_is_authoritative = False
        if (
            isinstance(run.execution_context_json, Mapping)
            and "request_id" in run.execution_context_json
        ):
            persisted_request_id = run.execution_context_json["request_id"]
            request_id_is_authoritative = True
        elif run.queue_request_id is not None:
            persisted_request_id = run.queue_request_id
            request_id_is_authoritative = True
        if (
            normalized_started["agent_id"] != run.agent_id
            or normalized_started["trace_id"] != run.trace_id
            or (
                request_id_is_authoritative
                and normalized_started.get("request_id") != persisted_request_id
            )
        ):
            raise ValueError("usage evidence does not match persisted run identity")
        result_json = {"started": normalized_started}
        values = {
            "id": str(uuid4()),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "usage_call_id": usage_call_id,
            "event_id": event_id,
            "operation_kind": operation_kind.value,
            "state": "started",
            "reserved_event_count": reserved_event_count,
            "result_json": result_json,
        }
        dialect_name = self._session.get_bind().dialect.name
        if dialect_name == "postgresql":
            statement = postgresql_insert(RunEvidenceOutboxModel).values(**values)
        elif dialect_name == "sqlite":
            statement = sqlite_insert(RunEvidenceOutboxModel).values(**values)
        else:  # pragma: no cover - 当前产品矩阵只支持 SQLite/PostgreSQL
            raise RuntimeError(f"unsupported usage settlement dialect: {dialect_name}")
        statement = statement.on_conflict_do_nothing(
            index_elements=["tenant_id", "usage_call_id"]
        ).returning(RunEvidenceOutboxModel.id)
        created_id = await self._session.scalar(statement)
        if created_id is not None:
            await EventCapacityRepository(self._session).reserve(
                run_id=run_id,
                operation_kind=operation_kind,
            )
            return UsageSettlementClaim(
                created=True,
                state="started",
                operation_kind=operation_kind.value,
                result_json=result_json,
                error_code=None,
            )

        existing = await self.get_usage(tenant_id=tenant_id, usage_call_id=usage_call_id)
        self._validate_usage_binding(
            existing,
            run_id=run_id,
            event_id=event_id,
            operation_kind=operation_kind.value,
            reserved_event_count=reserved_event_count,
            started_evidence=normalized_started,
        )
        return UsageSettlementClaim(
            created=False,
            state=existing.state,
            operation_kind=existing.operation_kind,
            result_json=existing.result_json,
            error_code=existing.error_code,
        )

    async def start_usage(
        self,
        *,
        tenant_id: str,
        run_id: str,
        usage_call_id: str,
        event_id: str,
        reserved_event_count: int,
        started_evidence: Mapping[str, object],
        operation_kind: str = "model_usage",
    ) -> RunEvidenceOutboxModel:
        if operation_kind not in {"model_usage", "embedding_usage"}:
            raise ValueError("invalid usage operation kind")
        normalized_started = _normalize_started_evidence(
            started_evidence,
            tenant_id=tenant_id,
            run_id=run_id,
            operation_kind=operation_kind,
        )
        existing = await self._session.scalar(
            select(RunEvidenceOutboxModel).where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
        )
        if existing is not None:
            self._validate_usage_binding(
                existing,
                run_id=run_id,
                event_id=event_id,
                operation_kind=operation_kind,
                reserved_event_count=reserved_event_count,
                started_evidence=normalized_started,
            )
            return existing
        model = RunEvidenceOutboxModel(
            id=str(uuid4()),
            tenant_id=tenant_id,
            run_id=run_id,
            usage_call_id=usage_call_id,
            event_id=event_id,
            operation_kind=operation_kind,
            state="started",
            reserved_event_count=reserved_event_count,
            result_json={"started": normalized_started},
        )
        self._session.add(model)
        await self._session.flush()
        return model

    @staticmethod
    def _validate_usage_binding(
        existing: RunEvidenceOutboxModel,
        *,
        run_id: str,
        event_id: str,
        operation_kind: str,
        reserved_event_count: int,
        started_evidence: Mapping[str, object],
    ) -> None:
        if existing.event_id != event_id:
            raise ValueError("usage call is bound to another event id")
        if (
            existing.run_id != run_id
            or existing.operation_kind != operation_kind
            or existing.reserved_event_count != reserved_event_count
        ):
            raise ValueError("usage call settlement does not match persisted operation")
        persisted_started = (
            existing.result_json.get("started")
            if isinstance(existing.result_json, Mapping)
            else None
        )
        if persisted_started != dict(started_evidence):
            difference_paths = _difference_paths(persisted_started, dict(started_evidence))
            fields = ",".join(difference_paths) or "unknown"
            raise ValueError(f"usage call is bound to another started identity fields={fields}")

    async def get_usage(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
    ) -> RunEvidenceOutboxModel:
        model = await self._session.scalar(
            select(RunEvidenceOutboxModel).where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
        )
        if model is None:
            raise LookupError("usage settlement not found")
        return model

    async def persist_result(
        self,
        *,
        tenant_id: str,
        usage_call_id: str,
        result: dict[str, Any] | None,
        error_code: str | None = None,
    ) -> RunEvidenceOutboxModel:
        model = await self._session.scalar(
            select(RunEvidenceOutboxModel)
            .where(
                RunEvidenceOutboxModel.tenant_id == tenant_id,
                RunEvidenceOutboxModel.usage_call_id == usage_call_id,
            )
            .with_for_update()
        )
        if model is None:
            raise LookupError("usage settlement not found")
        if model.operation_kind not in {
            EvidenceOperationKind.MODEL_USAGE.value,
            EvidenceOperationKind.EMBEDDING_USAGE.value,
        }:
            raise ValueError("settlement does not accept a usage result")
        if result is None or not isinstance(result.get("evidence"), Mapping):
            raise ValueError("usage result requires complete evidence")
        # 局部 import 避免 storage -> models -> events -> storage 的模块环。
        from agent_harness.models.usage import ModelUsageEvidence

        evidence = ModelUsageEvidence.model_validate(result["evidence"])
        expected_usage_kind = (
            "model"
            if model.operation_kind == EvidenceOperationKind.MODEL_USAGE.value
            else "embedding"
        )
        if (
            evidence.usage_kind != expected_usage_kind
            or evidence.tenant_id != model.tenant_id
            or evidence.run_id != model.run_id
        ):
            raise ValueError("usage evidence does not match persisted settlement")
        started_payload = (
            model.result_json.get("started") if isinstance(model.result_json, Mapping) else None
        )
        if not isinstance(started_payload, Mapping):
            raise RuntimeError("usage settlement is missing its durable started identity")
        started = ModelUsageEvidence.model_validate(started_payload)
        if (
            evidence.usage_kind != started.usage_kind
            or evidence.tenant_id != started.tenant_id
            or evidence.run_id != started.run_id
            or evidence.agent_id != started.agent_id
            or evidence.request_id != started.request_id
            or evidence.trace_id != started.trace_id
            or evidence.provider != started.provider
            or evidence.model != started.model
        ):
            raise ValueError("usage final identity does not match durable started identity")
        outcome = result.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise ValueError("usage result requires a non-empty outcome")
        normalized_result = {
            **result,
            "started": started.to_payload(),
            "evidence": evidence.to_payload(),
        }
        if model.state == "result_persisted":
            if model.result_json != normalized_result or model.error_code != error_code:
                raise RuntimeError("persisted usage result conflict")
            return model
        if model.state == "needs_review":
            raise RuntimeError("usage settlement needs_review cannot be closed automatically")
        if model.state != "started":
            raise RuntimeError(f"usage settlement cannot persist result from state: {model.state}")
        model.result_json = normalized_result
        model.error_code = error_code
        model.state = "result_persisted"
        await self._session.flush()
        return model

    async def pending_usage_run_ids(self) -> list[str]:
        """列出已有确定结果但尚未发布的 usage run，供进程启动恢复。"""

        result = await self._session.scalars(
            select(RunEvidenceOutboxModel.run_id)
            .where(
                RunEvidenceOutboxModel.operation_kind.in_(
                    (
                        EvidenceOperationKind.MODEL_USAGE.value,
                        EvidenceOperationKind.EMBEDDING_USAGE.value,
                    )
                ),
                RunEvidenceOutboxModel.state == "result_persisted",
            )
            .distinct()
        )
        return list(result.all())

    async def mark_published(self, *, tenant_id: str, usage_call_id: str) -> None:
        changed = cast(
            CursorResult[Any],
            await self._session.execute(
                update(RunEvidenceOutboxModel)
                .where(
                    RunEvidenceOutboxModel.tenant_id == tenant_id,
                    RunEvidenceOutboxModel.usage_call_id == usage_call_id,
                )
                .values(state="published")
            ),
        )
        if changed.rowcount != 1:
            raise LookupError("usage settlement not found")


__all__ = ["UsageEvidenceRepositoryMixin", "UsageSettlementClaim"]
