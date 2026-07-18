"""APR-002 CLI approve/deny 必须覆盖 delegated child 的终态聚合边界。"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from agent_harness.cli_access import resolve_approval
from agent_harness.delegation import DelegationRequest
from agent_harness.identity import IdentityContext
from agent_harness.runtime import RunStatus
from agent_harness.storage import run_migrations
from agent_harness.storage.repositories import RunCreate, SessionCreate
from app.runtime import build_runtime_components


def _service_copy(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "templates" / "service-app"
    target = tmp_path / "service-app"
    shutil.copytree(source, target)
    config = target / "agents" / "examples" / "basic" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "delegation_edges: []",
            "delegation_edges:\n  - examples.dev_assistant",
        ),
        encoding="utf-8",
    )
    return target


async def _prepare_waiting_child(
    *,
    service_root: Path,
    dsn: str,
    events_path: Path,
    decision: str,
) -> tuple[str, str, str]:
    components = build_runtime_components(
        profile="local",
        profiles_dir=service_root / "configs" / "profiles",
        storage_dsn=dsn,
        events_path=events_path,
        artifact_root=events_path.parent / "artifacts",
        workspace_root=service_root,
    )
    actor = IdentityContext.local_default()
    try:
        async with components.storage.uow() as uow:
            await uow.tenants.ensure(actor.tenant_id)
            session = await uow.sessions.ensure(
                SessionCreate(
                    session_id=actor.session_id,
                    tenant_id=actor.tenant_id,
                    user_id=actor.user_id,
                    agent_id="examples.basic",
                )
            )
            parent = await uow.runs.create(
                RunCreate(
                    tenant_id=actor.tenant_id,
                    session_id=session.id,
                    agent_id="examples.basic",
                    trace_id=f"trace-cli-{decision}",
                )
            )
            budget_runtime = cast(Any, components.executor_services["shared_budget"])
            await uow.shared_budget.create_ledger(
                budget_runtime.ledger_create(
                    tenant_id=actor.tenant_id,
                    run_id=parent.id,
                    agent_id="examples.basic",
                )
            )
            await uow.commit()
        delegated = await components.delegation_service.delegate(
            DelegationRequest(
                parent_run_id=parent.id,
                source_agent_id="examples.basic",
                target_agent_id="examples.dev_assistant",
                child_input={
                    "operation": "write",
                    "path": f"cli-{decision}.txt",
                    "content": "CLI delegation contract",
                },
                idempotency_key=f"cli-{decision}",
            ),
            identity=actor,
        )
        approvals = await components.approval_service.list_for_run(
            actor=actor,
            run_id=delegated.child_run_id,
        )
        assert len(approvals) == 1
        return parent.id, delegated.child_run_id, approvals[0].approval_id
    finally:
        await components.close()


async def _read_terminal_state(
    *,
    service_root: Path,
    dsn: str,
    events_path: Path,
    parent_run_id: str,
    child_run_id: str,
) -> tuple[str, str, bool]:
    components = build_runtime_components(
        profile="local",
        profiles_dir=service_root / "configs" / "profiles",
        storage_dsn=dsn,
        events_path=events_path,
        artifact_root=events_path.parent / "artifacts",
        workspace_root=service_root,
    )
    try:
        async with components.storage.uow() as uow:
            child = await uow.runs.get(child_run_id)
            aggregates = await uow.delegations.list_aggregates_for_parent(
                tenant_id="default",
                parent_run_id=parent_run_id,
            )
            delegation = await uow.delegations.get_by_child(child_run_id)
            assert delegation is not None
            reservation = await uow.delegations.get_reservation(delegation.id)
    finally:
        await components.close()

    assert child is not None
    assert len(aggregates) == 1
    assert reservation is not None
    return (
        child.status,
        aggregates[0].summary["children"][0]["status"],
        reservation.state == "needs_review",
    )


@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [
        ("approved", RunStatus.COMPLETED),
        ("denied", RunStatus.FAILED),
    ],
)
def test_cli_resolution_reconciles_delegated_child(
    tmp_path: Path,
    decision: Literal["approved", "denied"],
    expected_status: RunStatus,
) -> None:
    """CLI 真实重建 runtime 后仍结算 child；无 usage 时明确保留人工复核。"""

    service_root = _service_copy(tmp_path)
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'cli-{decision}.db'}"
    events_path = tmp_path / f"cli-{decision}.jsonl"
    run_migrations(dsn)
    parent_run_id, child_run_id, approval_id = asyncio.run(
        _prepare_waiting_child(
            service_root=service_root,
            dsn=dsn,
            events_path=events_path,
            decision=decision,
        )
    )

    resolve_approval(
        decision=decision,
        approval_id=approval_id,
        profile="local",
        profiles_dir=service_root / "configs" / "profiles",
        storage_dsn=dsn,
        events_path=events_path,
        agents_dir=service_root / "agents",
        comment="delegation CLI contract",
    )

    child_status, aggregate_status, needs_review = asyncio.run(
        _read_terminal_state(
            service_root=service_root,
            dsn=dsn,
            events_path=events_path,
            parent_run_id=parent_run_id,
            child_run_id=child_run_id,
        )
    )
    assert child_status == expected_status.value
    assert aggregate_status == expected_status.value
    assert needs_review is True
