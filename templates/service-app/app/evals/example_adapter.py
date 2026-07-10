"""把 approved file case 交给真实 registry/runtime/approval seam。"""

from __future__ import annotations

from typing import Any, cast

from agent_harness.approvals import ApprovalService, ApprovalStateConflict
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunOrchestrator, RunStatus
from agent_harness.security.redaction import redact_secrets
from agent_harness.storage import SQLAlchemyStorage


class ExampleEvalAdapter:
    """执行一个 approved case，并只投影声明参与比较的 typed 字段。"""

    def __init__(
        self,
        *,
        orchestrator: RunOrchestrator,
        approval_service: ApprovalService,
        storage: SQLAlchemyStorage,
        identity: IdentityContext,
    ) -> None:
        self._orchestrator = orchestrator
        self._approvals = approval_service
        self._storage = storage
        self._identity = identity

    async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
        """运行 agent、可选 resolve approval，并返回确定性比较 projection。"""

        payload = _payload(case)
        agent_id = str(case.get("agent_id") or "")
        input_payload = payload.get("input")
        if not agent_id or not isinstance(input_payload, dict):
            raise ValueError("approved example case requires agent_id and object payload.input")
        run = await self._orchestrator.start_run(
            agent_id=agent_id,
            input=cast(dict[str, Any], input_payload),
            identity=self._identity,
            request_id=f"eval:{case.get('case_id') or case.get('id') or 'case'}",
        )
        approval_status: str | None = None
        claim_state: str | None = None
        repeat_conflict = False
        if run.status == RunStatus.WAITING:
            rows = await self._approvals.list_for_run(
                actor=self._identity,
                run_id=run.run_id,
            )
            if not rows:
                raise RuntimeError("waiting example run did not create an approval record")
            approval = rows[0]
            decision = payload.get("approval_decision")
            if decision == "approved":
                resolved = await self._approvals.approve(
                    actor=self._identity,
                    run_id=run.run_id,
                    approval_id=approval.approval_id,
                )
                run = resolved.run or run
                approval_status = resolved.approval.status
                if payload.get("repeat_resolve") is True:
                    try:
                        await self._approvals.approve(
                            actor=self._identity,
                            run_id=run.run_id,
                            approval_id=approval.approval_id,
                        )
                    except ApprovalStateConflict:
                        repeat_conflict = True
            elif decision == "denied":
                resolved = await self._approvals.deny(
                    actor=self._identity,
                    run_id=run.run_id,
                    approval_id=approval.approval_id,
                )
                run = resolved.run or run
                approval_status = resolved.approval.status
            else:
                approval_status = approval.status
            async with self._storage.uow() as uow:
                claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
            if claim is not None:
                claim_state = claim.execution_state

        async with self._storage.uow() as uow:
            row = await uow.runs.get(run.run_id)
        if row is None:
            raise RuntimeError(f"example eval run disappeared: {run.run_id}")
        output = dict(row.output or {})
        evidence: dict[str, Any] = {
            **output,
            "run_status": row.status,
            "approval_status": approval_status,
            "claim_state": claim_state,
            "repeat_conflict": repeat_conflict,
            "trace_ref_present": _has_ref(output, "trace_ref", "model_trace_ref"),
            "artifact_ref_present": bool(output.get("artifact_ref")),
            "citation_count": len(output.get("citations", []))
            if isinstance(output.get("citations"), list)
            else 0,
        }
        raw_fields = payload.get("compare_fields")
        if not isinstance(raw_fields, list):
            raise ValueError("approved example case requires string payload.compare_fields")
        fields: list[str] = []
        for field in cast(list[object], raw_fields):
            if not isinstance(field, str):
                raise ValueError("approved example case requires string payload.compare_fields")
            fields.append(field)
        projected = {field: evidence.get(field) for field in fields}
        return cast(dict[str, Any], redact_secrets(projected))


def _payload(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("approved example case requires object payload")
    return cast(dict[str, Any], payload)


def _has_ref(output: dict[str, Any], *names: str) -> bool:
    return any(isinstance(output.get(name), str) and bool(output[name]) for name in names)
