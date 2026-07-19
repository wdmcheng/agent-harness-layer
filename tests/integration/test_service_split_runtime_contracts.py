"""真实 PostgreSQL/Redis/DBOS 的 service API/worker split 合同。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from tests.contracts.auth_policy_hitl_contract_helpers import asgi_request

from agent_harness.adapters.queue import RedisRunQueue
from agent_harness.auth import StaticTokenVerifier
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunStatus
from app.main import create_app
from app.runtime import build_runtime_components
from app.workers.runtime_worker import run_once

PROFILES = (
    Path(__file__).resolve().parents[2] / "templates" / "service-app" / "configs" / "profiles"
)


@pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="service split合同需要真实PostgreSQL与Redis。",
)
@pytest.mark.asyncio
async def test_service_submit_then_independent_worker_executes_same_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 API 仅提交 run，独立 worker 用持久身份和 trace 完成同一运行。"""

    postgres_dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    redis_dsn = os.environ["REDIS_TEST_DSN"]
    # TEST_POSTGRES_DSN 是pytest控制变量，不是 HarnessSettings 字段。
    monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    tenant_id = str(uuid4())
    trace_id = f"trace-service-split-{uuid4()}"
    identity = IdentityContext(
        tenant_id=tenant_id,
        user_id="api-user",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    api = build_runtime_components(
        profile="service", profiles_dir=PROFILES, storage_dsn=postgres_dsn
    )
    try:
        app = create_app(
            orchestrator=api.orchestrator,
            event_sink=api.event_sink,
            registry=api.registry,
            auth_verifier=StaticTokenVerifier({"split-token": identity}),
            policy_engine=api.policy_engine,
            input_guardrail=api.input_guardrail,
            approval_service=api.approval_service,
            eval_service=api.eval_service,
            experiment_service=api.experiment_service,
            acceptance_service=api.acceptance_service,
        )
        submit_status, submit_body = await asgi_request(
            cast(Any, app),
            method="POST",
            path="/api/v1/agents/examples.basic/runs",
            body={
                "input": {
                    "source_ref": "source://split",
                    "trust_level": "trusted",
                }
            },
            headers=[
                (b"authorization", b"Bearer split-token"),
                (b"x-trace-id", trace_id.encode("utf-8")),
            ],
        )
        assert submit_status == 202
        assert submit_body["status"] == RunStatus.CREATED.value
        submitted_run_id = submit_body["run_id"]
    finally:
        await api.close()

    worker_run_id = await run_once(
        profile="service", profiles_dir=PROFILES, storage_dsn=postgres_dsn
    )
    reader = build_runtime_components(
        profile="service", profiles_dir=PROFILES, storage_dsn=postgres_dsn
    )
    try:
        result = await reader.orchestrator.get_run(submitted_run_id, identity=identity)
        events = await reader.event_sink.read(run_id=submitted_run_id)
    finally:
        assert isinstance(reader.queue, RedisRunQueue)
        await reader.queue.cleanup_namespace()
        await reader.close()

    assert worker_run_id == submitted_run_id
    assert result.status == RunStatus.COMPLETED
    assert [event.event_type.value for event in events] == [
        "run.queued",
        "input.guardrail.checked",
        "run.started",
        "run.completed",
    ]
    for event in events:
        assert event.request_id == "req-auth-policy-hitl"
        assert event.trace_id == trace_id
        assert event.payload is not None
        assert event.payload["source_ref"] == "source://split"
        assert event.payload["trust_level"] == "trusted"


@pytest.mark.skipif(
    not (os.environ.get("AGENT_HARNESS_TEST_POSTGRES_DSN") and os.environ.get("REDIS_TEST_DSN")),
    reason="service approval合同需要真实PostgreSQL与Redis。",
)
@pytest.mark.asyncio
async def test_service_approval_is_queued_and_worker_resumes_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """验证审批 HTTP 返回 waiting 后由 worker 恢复一次，审计注释同时完成脱敏。"""

    postgres_dsn = os.environ["AGENT_HARNESS_TEST_POSTGRES_DSN"]
    redis_dsn = os.environ["REDIS_TEST_DSN"]
    monkeypatch.delenv("AGENT_HARNESS_TEST_POSTGRES_DSN")
    monkeypatch.setenv("AGENT_HARNESS_QUEUE__DSN", redis_dsn)
    cleanup = RedisRunQueue.from_dsn(redis_dsn)
    await cleanup.cleanup_namespace()
    await cleanup.close()
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    workspace.mkdir()
    tenant_id = str(uuid4())
    submitter = IdentityContext(
        tenant_id=tenant_id,
        user_id="submitter",
        session_id=str(uuid4()),
        roles=["admin"],
        permissions=["*"],
        auth_method="api-key",
    )
    reviewer = IdentityContext(
        tenant_id=tenant_id,
        user_id="reviewer",
        session_id=str(uuid4()),
        roles=["reviewer"],
        permissions=["*"],
        auth_method="api-key",
    )
    api = build_runtime_components(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=postgres_dsn,
        artifact_root=artifacts,
        workspace_root=workspace,
    )
    try:
        submitted = await api.orchestrator.submit_run(
            agent_id="examples.dev_assistant",
            input={"operation": "write", "path": "approved.txt", "content": "once"},
            identity=submitter,
            request_id="req-submit-approval",
        )
    finally:
        await api.close()

    await run_once(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=postgres_dsn,
        artifact_root=artifacts,
        workspace_root=workspace,
    )
    approval_api = build_runtime_components(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=postgres_dsn,
        artifact_root=artifacts,
        workspace_root=workspace,
    )
    try:
        approvals = await approval_api.approval_service.list_for_run(
            actor=submitter, run_id=submitted.run_id
        )
        assert len(approvals) == 1
        approval = approvals[0]
        app = create_app(
            orchestrator=approval_api.orchestrator,
            event_sink=approval_api.event_sink,
            registry=approval_api.registry,
            auth_verifier=StaticTokenVerifier({"review-token": reviewer}),
            policy_engine=approval_api.policy_engine,
            input_guardrail=approval_api.input_guardrail,
            approval_service=approval_api.approval_service,
            eval_service=approval_api.eval_service,
            experiment_service=approval_api.experiment_service,
            acceptance_service=approval_api.acceptance_service,
        )
        approve_status, approve_body = await asgi_request(
            cast(Any, app),
            method="POST",
            path=f"/api/v1/runs/{submitted.run_id}/approvals/{approval.approval_id}",
            body={
                "decision": "approved",
                "comment": "reviewed sk-abcdef1234567890",
            },
            headers=[(b"authorization", b"Bearer review-token")],
        )
        assert approve_status == 202
        assert approve_body["approval"]["status"] == "waiting"
    finally:
        await approval_api.close()

    await run_once(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=postgres_dsn,
        artifact_root=artifacts,
        workspace_root=workspace,
    )
    reader = build_runtime_components(
        profile="service",
        profiles_dir=PROFILES,
        storage_dsn=postgres_dsn,
        artifact_root=artifacts,
        workspace_root=workspace,
    )
    try:
        result = await reader.orchestrator.get_run(submitted.run_id, identity=submitter)
        resolved = await reader.approval_service.get(
            actor=submitter,
            run_id=submitted.run_id,
            approval_id=approval.approval_id,
        )
        events = await reader.event_sink.read(run_id=submitted.run_id)
        async with reader.storage.uow() as uow:
            audits = await uow.audit_logs.list_for_tenant(tenant_id)
    finally:
        assert isinstance(reader.queue, RedisRunQueue)
        await reader.queue.cleanup_namespace()
        await reader.close()

    assert result.status == RunStatus.COMPLETED
    assert resolved.status == "approved"
    assert resolved.resolved_by == "reviewer"
    assert (workspace / "approved.txt").read_text(encoding="utf-8") == "once"
    assert sum(event.event_type.value == "approval.resolved" for event in events) == 1
    approved_audit = [row for row in audits if row.action == "approval.approved"]
    assert len(approved_audit) == 1
    audit_comment = str(approved_audit[0].payload["evidence"]["comment"])
    assert "reviewed" in audit_comment
    assert "sk-abcdef1234567890" not in audit_comment
