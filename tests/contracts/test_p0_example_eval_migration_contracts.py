"""四个示例 eval 数据集与审批 claim migration 合同测试。"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command

from agent_harness.identity import IdentityContext
from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import alembic_config, get_current_revision
from app.runtime import RuntimeComponents, build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"


def _dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _components(tmp_path: Path, *, name: str) -> tuple[RuntimeComponents, Path]:
    db_path = tmp_path / f"{name}.db"
    components = build_runtime_components(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(db_path),
        events_path=tmp_path / f"{name}-events.jsonl",
        artifact_root=tmp_path / f"{name}-artifacts",
    )
    return components, db_path


def test_four_approved_datasets_run_deterministically(tmp_path: Path) -> None:
    """四个 dataset 真实执行，draft 跳过，score/trace evidence 本地落盘。"""

    result = subprocess.run(
        [
            sys.executable,
            str(SERVICE_APP / "scripts" / "run_example_evals.py"),
            "--state-dir",
            str(tmp_path / "eval-state"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for agent_id, case_count in {
        "examples.rag_assistant": 3,
        "examples.ticket_triage": 2,
        "examples.repo_analyst": 2,
        "examples.dev_assistant": 4,
    }.items():
        assert f"agent={agent_id} status=completed cases={case_count}" in result.stdout
    assert "agent=examples.rag_assistant" in result.stdout
    assert "drafts_skipped=1" in result.stdout
    assert "example-eval: status=ok failures=0" in result.stdout
    assert (tmp_path / "eval-state" / "scores.jsonl").exists()
    assert (tmp_path / "eval-state" / "traces.jsonl").exists()


@pytest.mark.asyncio
async def test_0008_downgrade_only_allows_empty_disposable_data(tmp_path: Path) -> None:
    """0008 空数据可逆；任一 resolution/claim 数据存在时必须拒绝。"""

    empty_dsn = _dsn(tmp_path / "empty-downgrade.db")
    run_migrations(empty_dsn)
    await asyncio.to_thread(
        command.downgrade,
        alembic_config(empty_dsn),
        "0007_eval_gate_trace_loop",
    )
    assert await asyncio.to_thread(get_current_revision, empty_dsn) == "0007_eval_gate_trace_loop"

    components, db_path = _components(tmp_path, name="nonempty-downgrade")
    try:
        waiting = await components.orchestrator.start_run(
            agent_id="examples.dev_assistant",
            input={"operation": "shell", "command": "echo migration"},
        )
        approval = (
            await components.approval_service.list_for_run(
                actor=IdentityContext.local_default(),
                run_id=waiting.run_id,
            )
        )[0]
        async with components.storage.uow() as uow:
            await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=approval.run_id,
                tenant_id=approval.tenant_id,
            )
            await uow.commit()
    finally:
        await components.close()

    with pytest.raises(RuntimeError, match="downgrade refused"):
        await asyncio.to_thread(
            command.downgrade,
            alembic_config(_dsn(db_path)),
            "0007_eval_gate_trace_loop",
        )
    assert (
        await asyncio.to_thread(get_current_revision, _dsn(db_path))
        == "0008_agent_execution_approval_claims"
    )
