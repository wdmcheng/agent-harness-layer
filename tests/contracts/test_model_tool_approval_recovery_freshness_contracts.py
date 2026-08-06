"""模型工具审批恢复的真实lease新鲜度与终态映射合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn
from tests.contracts.test_policy_gated_model_tool_loop_sqlite_resume_contracts import (
    _build_runtime,  # pyright: ignore[reportPrivateUsage]
)
from tests.contracts.test_tool_intent_usage_settlement_contracts import (
    _ToolIntentProvider,  # pyright: ignore[reportPrivateUsage]
)

from agent_harness.artifacts import FileArtifactStore
from agent_harness.identity import IdentityContext
from agent_harness.runtime import ApprovalGrant, ModelToolLoopApprovalStore, ModelToolLoopError
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from agent_harness.storage.models import ApprovalModel


async def _waiting_lease(
    storage: SQLAlchemyStorage,
    *,
    tmp_path: Path,
) -> tuple[ApprovalGrant, list[dict[str, Any]], _ToolIntentProvider]:
    """经公开orchestrator建立waiting快照，再由repository取得真实resolution lease。"""

    identity = IdentityContext.local_default(session_id="approval-freshness")
    effects: list[dict[str, Any]] = []
    approvals, orchestrator, provider = _build_runtime(
        storage=storage,
        tmp_path=tmp_path,
        identity=identity,
        final_text=False,
        handler_effects=effects,
    )
    waiting = await orchestrator.start_run(
        agent_id="agent-a",
        input={"prompt": "use search"},
    )
    records = await approvals.list_for_run(actor=identity, run_id=waiting.run_id)
    assert len(records) == 1
    approval = records[0]
    async with storage.uow() as uow:
        lease = await uow.approvals.claim_resolution(
            approval_id=approval.approval_id,
            run_id=waiting.run_id,
            tenant_id=identity.tenant_id,
            request_id="approval-freshness-request",
        )
        await uow.commit()
    return (
        ApprovalGrant(
            approval_id=approval.approval_id,
            lease_id=lease.lease_id,
            tenant_id=approval.tenant_id,
            identity_id=identity.user_id,
            session_id=identity.session_id,
            agent_id=approval.agent_id,
            run_id=approval.run_id,
            action=approval.action,
            resource=approval.resource,
            arguments_hash=str(approval.metadata["arguments_hash"]),
        ),
        effects,
        provider,
    )


@pytest.mark.asyncio
async def test_active_fresh_grant_resolves_but_stale_lease_is_expired(tmp_path: Path) -> None:
    """同一artifact只接受新鲜active lease；超时grant不能触发handler或模型续跑。"""

    dsn = sqlite_dsn(tmp_path / "approval-freshness.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        grant, effects, provider = await _waiting_lease(storage, tmp_path=tmp_path)
        async with storage.uow() as uow:
            lease = await uow.approvals.get_resolution(grant.approval_id)
        assert lease is not None and lease.claimed_at is not None
        claimed_at = lease.claimed_at.replace(tzinfo=UTC)
        fresh_store = ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
            trusted_clock=lambda: claimed_at + timedelta(seconds=29),
            max_grant_age_seconds=30,
        )
        snapshot = await fresh_store.resolve(grant=grant)
        assert snapshot.intent.tool_call_id
        assert effects == []
        assert provider.send_count == 1

        stale_store = ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
            trusted_clock=lambda: claimed_at + timedelta(seconds=31),
            max_grant_age_seconds=30,
        )
        with pytest.raises(ModelToolLoopError) as failure:
            await stale_store.resolve(grant=grant)
        assert failure.value.code == "approval.expired"
        assert effects == []
        assert provider.send_count == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "state", "expected_code"),
    [
        ("denied", "denied", "approval.denied"),
        ("waiting", "revoked", "approval.revoked"),
        ("approved", "completed", "approval.invalid_transition"),
    ],
    ids=("denied", "revoked", "grant-consumed"),
)
async def test_non_active_or_consumed_grant_never_resolves_snapshot(
    tmp_path: Path,
    status: str,
    state: str,
    expected_code: str,
) -> None:
    """拒绝、撤销和已消费grant都在artifact读取与approved handler前关闭。"""

    dsn = sqlite_dsn(tmp_path / f"approval-{state}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        grant, effects, provider = await _waiting_lease(storage, tmp_path=tmp_path)
        async with storage.uow() as uow:
            await uow.session.execute(
                update(ApprovalModel)
                .where(ApprovalModel.id == grant.approval_id)
                .values(status=status, resolution_state=state)
            )
            await uow.commit()
        store = ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
            trusted_clock=lambda: datetime.now(UTC),
            max_grant_age_seconds=300,
        )
        with pytest.raises(ModelToolLoopError) as failure:
            await store.resolve(grant=grant)
        assert failure.value.code == expected_code
        assert effects == []
        assert provider.send_count == 1
    finally:
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ("forged_grant_session", "missing_metadata_session", "missing_continuation_session"),
)
async def test_durable_approval_session_binding_fails_closed_before_snapshot_execution(
    tmp_path: Path,
    mutation: str,
) -> None:
    """真实SQLite lease、metadata、continuation任一session漂移都在恢复副作用前关闭。"""

    dsn = sqlite_dsn(tmp_path / f"approval-session-{mutation}.sqlite3")
    run_migrations(dsn)
    storage = SQLAlchemyStorage(dsn)
    try:
        grant, effects, provider = await _waiting_lease(storage, tmp_path=tmp_path)
        candidate = grant
        if mutation == "forged_grant_session":
            candidate = grant.model_copy(update={"session_id": "cross-session-forgery"})
        else:
            async with storage.uow() as uow:
                approval = await uow.approvals.get(grant.approval_id)
                assert approval is not None
                metadata = dict(approval.metadata)
                if mutation == "missing_metadata_session":
                    metadata.pop("session_id")
                else:
                    continuation = dict(metadata["continuation"])
                    continuation.pop("session_id")
                    metadata["continuation"] = continuation
                await uow.session.execute(
                    update(ApprovalModel)
                    .where(ApprovalModel.id == grant.approval_id)
                    .values(metadata_json=metadata)
                )
                await uow.commit()

        store = ModelToolLoopApprovalStore(
            storage=storage,
            artifact_store=FileArtifactStore(tmp_path / "artifacts"),
            trusted_clock=lambda: datetime.now(UTC),
            max_grant_age_seconds=300,
        )
        with pytest.raises(ModelToolLoopError) as failure:
            await store.resolve(grant=candidate)
        assert failure.value.code == "tool.approval_invalid"
        assert effects == []
        assert provider.send_count == 1
    finally:
        await storage.dispose()
