"""Tool-intent 复用既有 model.invoke Policy/HITL 状态机的组合合同。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.contracts.controlled_real_model_policy_approval_test_support import policy_flow

from agent_harness.approvals import ApprovalStateConflict
from agent_harness.models import ModelRequest
from agent_harness.runtime import ApprovalGrant, RunStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deny", "expected_status", "expected_sends"),
    [
        (False, RunStatus.COMPLETED, 1),
        (True, RunStatus.FAILED, 0),
    ],
)
async def test_tool_intent_policy_allow_or_deny_precedes_every_provider_side_effect(
    tmp_path: Path,
    deny: bool,
    expected_status: RunStatus,
    expected_sends: int,
) -> None:
    """Policy 三态中的 allow/deny 必须分别单发或在 prepare 前关闭失败。"""

    stem = f"tool-intent-{'deny' if deny else 'allow'}"
    storage, _approval, orchestrator, _identity, provider, _executor = await policy_flow(
        tmp_path,
        require_approval=False,
        deny=deny,
        database_stem=stem,
        tool_intent=True,
    )
    try:
        result = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        events = (tmp_path / f"{stem}-events.jsonl").read_text(encoding="utf-8")

        assert result.status == expected_status
        assert provider.prepare_count == expected_sends
        assert provider.send_count == expected_sends
        assert "tool.call." not in events
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_intent_waiting_freezes_identity_with_zero_provider_side_effects(
    tmp_path: Path,
) -> None:
    """require-approval 必须冻结完整 turn 身份，且等待期不创建 provider 副作用。"""

    storage, approval, orchestrator, identity, provider, _executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="tool-intent-waiting",
        tool_intent=True,
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        records = await approval.list_for_run(actor=identity, run_id=waiting.run_id)
        continuation = records[0].metadata["continuation"]

        assert waiting.status == RunStatus.WAITING
        assert len(records) == 1
        assert records[0].action == "model.invoke"
        assert continuation["kind"] == "tool_intent_policy_approval"
        assert continuation["tool_intent_replay_seed"]["schema_version"] == (
            "tool-intent-replay-seed-v1"
        )
        assert continuation["usage_call_id"]
        assert provider.prepare_count == 0
        assert provider.send_count == 0
        async with storage.uow() as uow:
            usage_rows = [
                row
                for row in await uow.evidence_outbox.pending(run_id=waiting.run_id)
                if row.operation_kind == "model_usage"
            ]
        assert usage_rows == []
        assert "tool.call." not in (tmp_path / "tool-intent-waiting-events.jsonl").read_text(
            encoding="utf-8"
        )
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper_kind", ["turn", "catalog"])
async def test_synchronized_tool_recovery_tamper_fails_before_provider(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    """同步改写 turn 或 catalog bytes 仍与冻结耐久请求身份冲突。"""

    stem = f"tool-intent-{tamper_kind}-tamper"
    database = tmp_path / f"{stem}.db"
    storage, approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem=stem,
        tool_intent=True,
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        assert executor.bound_model is not None
        with sqlite3.connect(database) as connection:
            metadata = json.loads(
                connection.execute(
                    "select metadata_json from approvals where id = ?",
                    (record.approval_id,),
                ).fetchone()[0]
            )
            state = json.loads(
                connection.execute(
                    "select state_json from checkpoints where run_id = ?",
                    (waiting.run_id,),
                ).fetchone()[0]
            )
            if tamper_kind == "turn":
                metadata["continuation"]["tool_intent_replay_seed"]["turn_ordinal"] = 2
                state["continuation"]["tool_intent_replay_seed"]["turn_ordinal"] = 2
            else:
                metadata["continuation"]["tool_intent_replay_seed"][
                    "provider_tool_catalog_json"
                ] = '{"schema_version":"provider-tool-catalog-v1","tools":[]}'
                state["continuation"]["tool_intent_replay_seed"]["provider_tool_catalog_json"] = (
                    '{"schema_version":"provider-tool-catalog-v1","tools":[]}'
                )
            connection.execute(
                "update approvals set metadata_json = ? where id = ?",
                (json.dumps(metadata), record.approval_id),
            )
            connection.execute(
                "update checkpoints set state_json = ? where run_id = ?",
                (json.dumps(state), waiting.run_id),
            )
            connection.commit()
        async with storage.uow() as uow:
            lease = await uow.approvals.claim_resolution(
                approval_id=record.approval_id,
                run_id=record.run_id,
                tenant_id=record.tenant_id,
                request_id="tamper-claim",
            )
            await uow.commit()
        grant = ApprovalGrant(
            approval_id=record.approval_id,
            lease_id=lease.lease_id,
            tenant_id=record.tenant_id,
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id=record.agent_id,
            run_id=record.run_id,
            action=record.action,
            resource=record.resource,
            arguments_hash=str(record.metadata["arguments_hash"]),
        )

        with pytest.raises(ValueError, match="tool-intent approval"):
            await executor.bound_model.complete_tool_intent_approved(
                ModelRequest(
                    deployment_id="real_primary",
                    provider="openai-compatible",
                    model="fixture-text-1",
                    prompt="需要审批",
                    capability="tool_intent",
                    max_output_tokens=2,
                ),
                operation_key="forged-operation-key",
                grant=grant,
            )

        assert provider.prepare_count == provider.send_count == 0
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_matching_tool_intent_approval_resumes_once_with_frozen_identity(
    tmp_path: Path,
) -> None:
    """匹配 grant 只续跑一次同一 turn，并且 provider prepare/send 均不超过一次。"""

    storage, approval, orchestrator, identity, provider, executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="tool-intent-approved",
        tool_intent=True,
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        resolved = await approval.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-tool-intent-once",
        )

        assert resolved.run is not None
        assert resolved.run.status == RunStatus.COMPLETED
        assert executor.resume_calls == 1
        assert provider.prepare_count == 1
        assert provider.send_count == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_tool_intent_approval_replay_after_completion_is_fenced(
    tmp_path: Path,
) -> None:
    """审批完成后的重复投递只命中既有 lease fence，不得再次发送 provider。"""

    storage, approval, orchestrator, identity, provider, _executor = await policy_flow(
        tmp_path,
        require_approval=True,
        database_stem="tool-intent-approval-replay",
        tool_intent=True,
    )
    try:
        waiting = await orchestrator.start_run(agent_id="agent-a", input={"prompt": "x"})
        record = (await approval.list_for_run(actor=identity, run_id=waiting.run_id))[0]
        first = await approval.approve(
            actor=identity,
            run_id=waiting.run_id,
            approval_id=record.approval_id,
            request_id="approve-first",
        )
        with pytest.raises(ApprovalStateConflict):
            await approval.approve(
                actor=identity,
                run_id=waiting.run_id,
                approval_id=record.approval_id,
                request_id="approve-crash-replay",
            )

        assert first.run is not None
        assert first.run.status == RunStatus.COMPLETED
        assert provider.prepare_count == provider.send_count == 1
    finally:
        await storage.dispose()
