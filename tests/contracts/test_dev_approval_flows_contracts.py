"""Dev assistant 审批、重启恢复、并发与已知失败合同测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.events import LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.runtime import ApprovalGrant, InvalidRunTransition, RunStatus
from agent_harness.storage import run_migrations
from app.main import create_app
from app.runtime import RuntimeComponents, build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"


def _dsn(path: Path) -> str:
    """将临时数据库文件转换为审批流程 runtime 使用的异步 SQLite DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _components(
    tmp_path: Path,
    *,
    name: str,
    workspace_root: Path | None = None,
) -> tuple[RuntimeComponents, Path, Path]:
    """创建隔离的本地 runtime、数据库和事件文件，供审批流程断言复用。"""

    db_path = tmp_path / f"{name}.db"
    events_path = tmp_path / f"{name}-events.jsonl"
    run_migrations(_dsn(db_path))
    components = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(db_path),
        events_path=events_path,
        artifact_root=tmp_path / f"{name}-artifacts",
        workspace_root=workspace_root,
    )
    return components, db_path, events_path


@pytest.mark.asyncio
async def test_dev_approval_restarts_without_public_resume_bypass_or_replay(
    tmp_path: Path,
) -> None:
    """waiting run 重启后由不同 reviewer approve，文件动作只执行一次。"""

    workspace = tmp_path / "dev-workspace"
    workspace.mkdir()
    components, db_path, events_path = _components(
        tmp_path,
        name="dev-restart",
        workspace_root=workspace,
    )
    waiting = await components.orchestrator.start_run(
        agent_id="examples.dev_assistant",
        input={"operation": "write", "path": "approved.txt", "content": "written once"},
    )
    assert waiting.status == RunStatus.WAITING and waiting.resume_token is not None
    assert not (workspace / "approved.txt").exists()
    app = create_app(
        orchestrator=components.orchestrator,
        event_sink=components.event_sink,
        registry=components.registry,
        policy_engine=components.policy_engine,
        input_guardrail=components.input_guardrail,
        approval_service=components.approval_service,
        eval_service=components.eval_service,
        profile="local",
        profiles_dir=PROFILES,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/runs/{waiting.run_id}/resume",
            json={"resume_token": waiting.resume_token.value},
            headers={"X-Request-Id": "req-public-resume-rejected"},
        )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "run.invalid_transition",
        "message": "executor approval resume requires ApprovalGrant",
        "request_id": "req-public-resume-rejected",
    }
    with pytest.raises(InvalidRunTransition):
        await components.orchestrator.resume_run(
            waiting.resume_token,
            expected_run_id=waiting.run_id,
        )
    async with components.storage.uow() as uow:
        still_waiting = await uow.runs.get(waiting.run_id)
        first_approval = (await uow.approvals.list_by_run(waiting.run_id))[0]
    assert still_waiting is not None and still_waiting.status == "waiting"
    assert first_approval.status == "waiting"
    await components.close()

    restarted = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(db_path),
        events_path=events_path,
        artifact_root=tmp_path / "dev-restart-artifacts",
        workspace_root=workspace,
    )
    reviewer = IdentityContext(
        tenant_id="default",
        user_id="security-reviewer",
        session_id="review-session",
        roles=["reviewer"],
        permissions=["*"],
        auth_method="test",
    )
    try:
        resolved = await restarted.approval_service.approve(
            actor=reviewer,
            run_id=waiting.run_id,
            approval_id=first_approval.approval_id,
        )
        with pytest.raises(ApprovalStateConflict):
            await restarted.approval_service.approve(
                actor=reviewer,
                run_id=waiting.run_id,
                approval_id=first_approval.approval_id,
            )
        async with restarted.storage.uow() as uow:
            approval = await uow.approvals.get(first_approval.approval_id)
            claim = await uow.tool_invocations.get_by_approval_id(first_approval.approval_id)
    finally:
        await restarted.close()

    events = await LocalJsonlEventSink(events_path).read(run_id=waiting.run_id)
    assert resolved.run is not None and resolved.run.status == RunStatus.COMPLETED
    assert (workspace / "approved.txt").read_text(encoding="utf-8") == "written once"
    assert approval is not None and approval.status == "approved"
    assert approval.resolved_by == "security-reviewer"
    assert claim is not None and claim.execution_state == "completed"
    assert sum(event.terminal for event in events) == 1


@pytest.mark.asyncio
async def test_dev_deny_and_known_tool_failure_keep_approval_semantics(tmp_path: Path) -> None:
    """deny 不执行；已允许的确定性失败使 run failed 但 approval approved。"""

    workspace = tmp_path / "dev-outcomes-workspace"
    workspace.mkdir()
    components, _, _ = _components(
        tmp_path,
        name="dev-outcomes",
        workspace_root=workspace,
    )
    try:
        denied_run = await components.orchestrator.start_run(
            agent_id="examples.dev_assistant",
            input={"operation": "write", "path": "denied.txt", "content": "must not exist"},
        )
        denied_approval = (
            await components.approval_service.list_for_run(
                actor=IdentityContext.local_default(),
                run_id=denied_run.run_id,
            )
        )[0]
        denied = await components.approval_service.deny(
            actor=IdentityContext.local_default(),
            run_id=denied_run.run_id,
            approval_id=denied_approval.approval_id,
        )

        failed_run = await components.orchestrator.start_run(
            agent_id="examples.dev_assistant",
            input={
                "operation": "shell",
                "command": "python -c 'import sys; sys.exit(3)'",
            },
        )
        failed_approval = (
            await components.approval_service.list_for_run(
                actor=IdentityContext.local_default(),
                run_id=failed_run.run_id,
            )
        )[0]
        failed = await components.approval_service.approve(
            actor=IdentityContext.local_default(),
            run_id=failed_run.run_id,
            approval_id=failed_approval.approval_id,
        )
        async with components.storage.uow() as uow:
            failed_claim = await uow.tool_invocations.get_by_approval_id(
                failed_approval.approval_id
            )
            failed_private = await uow.approvals.get_resolution(failed_approval.approval_id)
            audit_records = await uow.audit_logs.list_for_tenant("default")
    finally:
        await components.close()

    assert denied.approval.status == "denied"
    assert denied.run is not None and denied.run.status == RunStatus.FAILED
    assert not (workspace / "denied.txt").exists()
    denied_audits = [record for record in audit_records if record.action == "approval.denied"]
    assert len(denied_audits) == 1
    assert denied_audits[0].payload["run_id"] == denied_run.run_id
    assert denied_audits[0].payload["evidence"]["approval_id"] == denied_approval.approval_id
    assert failed.approval.status == "approved"
    assert failed.run is not None and failed.run.status == RunStatus.FAILED
    assert failed_claim is not None and failed_claim.execution_state == "failed"
    assert failed_private is not None and failed_private.state == "failed"


@pytest.mark.asyncio
async def test_concurrent_approve_creates_one_lease_claim_and_terminal(tmp_path: Path) -> None:
    """并发 approve 只有一个成功，unique claim 和 terminal 都保持单一。"""

    workspace = tmp_path / "concurrent-workspace"
    workspace.mkdir()
    components, _, events_path = _components(
        tmp_path,
        name="concurrent-approve",
        workspace_root=workspace,
    )
    try:
        waiting = await components.orchestrator.start_run(
            agent_id="examples.dev_assistant",
            input={"operation": "write", "path": "once.txt", "content": "one execution"},
        )
        approval = (
            await components.approval_service.list_for_run(
                actor=IdentityContext.local_default(),
                run_id=waiting.run_id,
            )
        )[0]
        outcomes = await asyncio.gather(
            components.approval_service.approve(
                actor=IdentityContext.local_default(),
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            ),
            components.approval_service.approve(
                actor=IdentityContext.local_default(),
                run_id=waiting.run_id,
                approval_id=approval.approval_id,
            ),
            return_exceptions=True,
        )
        async with components.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
            persisted = await uow.approvals.get(approval.approval_id)
    finally:
        await components.close()

    events = await LocalJsonlEventSink(events_path).read(run_id=waiting.run_id)
    assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
    assert sum(isinstance(item, ApprovalStateConflict) for item in outcomes) == 1
    assert claim is not None and claim.execution_state == "completed"
    assert persisted is not None and persisted.status == "approved"
    assert (workspace / "once.txt").read_text(encoding="utf-8") == "one execution"
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type.value == "approval.resolved" for event in events) == 1


@pytest.mark.asyncio
async def test_deterministic_run_result_before_approval_finalize_recovers_existing_claim(
    tmp_path: Path,
) -> None:
    """tool result 已落库但 ordered evidence 未完成时只读既有 claim 收口。"""

    workspace = tmp_path / "finalize-window-workspace"
    workspace.mkdir()
    components, _, events_path = _components(
        tmp_path,
        name="finalize-window",
        workspace_root=workspace,
    )
    try:
        waiting = await components.orchestrator.start_run(
            agent_id="examples.dev_assistant",
            input={"operation": "write", "path": "window.txt", "content": "persisted"},
        )
        approval = (
            await components.approval_service.list_for_run(
                actor=IdentityContext.local_default(),
                run_id=waiting.run_id,
            )
        )[0]
        async with components.storage.uow() as uow:
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
                request_id="req-dev-approval-lease",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=approval.tenant_id,
            identity_id=str(approval.metadata["identity_id"]),
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=str(approval.metadata["arguments_hash"]),
        )
        terminal = await components.orchestrator.resume_run(
            approval.resume_token or "",
            expected_run_id=approval.run_id,
            identity=IdentityContext.local_default(),
            approval_grant=grant,
            defer_terminal=True,
        )
        async with components.storage.uow() as uow:
            before_finalize = await uow.approvals.get(approval.approval_id)
        recovered = await components.approval_service.recover_claimed(
            actor=IdentityContext.local_default(),
            run_id=approval.run_id,
            approval_id=approval.approval_id,
        )
        async with components.storage.uow() as uow:
            claim = await uow.tool_invocations.get_by_approval_id(approval.approval_id)
    finally:
        await components.close()

    events = await LocalJsonlEventSink(events_path).read(run_id=waiting.run_id)
    assert terminal.status == RunStatus.COMPLETED
    assert before_finalize is not None and before_finalize.status == "waiting"
    assert recovered.approval.status == "approved"
    assert claim is not None and claim.execution_state == "completed"
    assert (workspace / "window.txt").read_text(encoding="utf-8") == "persisted"
    assert sum(event.terminal for event in events) == 1
    assert sum(event.event_type.value == "approval.resolved" for event in events) == 1
