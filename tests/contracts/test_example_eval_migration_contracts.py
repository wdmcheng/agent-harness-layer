"""四个示例 eval 数据集与审批 claim migration 合同测试。"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from agent_harness.identity import IdentityContext
from agent_harness.storage import run_migrations
from agent_harness.storage.migrations.runner import alembic_config, get_current_revision
from app.runtime import RuntimeComponents, build_runtime_components

ROOT = Path(__file__).resolve().parents[2]
SERVICE_APP = ROOT / "templates" / "service-app"
PROFILES = SERVICE_APP / "configs" / "profiles"


@pytest.mark.parametrize(
    ("target", "state_name"),
    [
        ("eval", "eval"),
        ("eval-rag", "eval-rag"),
        ("eval-ticket", "eval-ticket"),
        ("eval-repo", "eval-repo"),
        ("eval-dev", "eval-dev"),
    ],
)
def test_template_eval_targets_migrate_fresh_database_before_runtime(
    tmp_path: Path,
    target: str,
    state_name: str,
) -> None:
    """全新复制项目的每个 eval target 都先显式迁移自己的库。"""

    result = subprocess.run(
        ["make", "-n", target, f"STATE_DIR={tmp_path}"],
        cwd=SERVICE_APP,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    migration = "python app/migrate.py"
    evaluation = "python scripts/run_example_evals.py"
    assert migration in result.stdout
    assert result.stdout.index(migration) < result.stdout.index(evaluation)
    assert f"sqlite+aiosqlite:///{tmp_path}/{state_name}/eval.db" in result.stdout


def _dsn(path: Path) -> str:
    """将临时 SQLite 文件转换为示例 runtime 组件使用的异步 DSN。"""

    return f"sqlite+aiosqlite:///{path}"


def _downgrade_config(dsn: str) -> Config:
    """构造允许空证据回退的 Alembic 配置，供可逆性边界测试使用。"""

    config = alembic_config(dsn)
    config.cmd_opts = Namespace(x=["allow_empty_evidence_downgrade=true"])
    return config


def _components(tmp_path: Path, *, name: str) -> tuple[RuntimeComponents, Path]:
    """为迁移场景创建独立 runtime 与数据库，避免真实组件共享状态。"""

    db_path = tmp_path / f"{name}.db"
    run_migrations(_dsn(db_path))
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

    state_dir = tmp_path / "eval-state"
    state_dir.mkdir()
    run_migrations(_dsn(state_dir / "eval.db"))
    result = subprocess.run(
        [
            sys.executable,
            str(SERVICE_APP / "scripts" / "run_example_evals.py"),
            "--state-dir",
            str(state_dir),
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
    assert (state_dir / "scores.jsonl").exists()
    assert (state_dir / "traces.jsonl").exists()


def test_example_eval_uses_fake_model_without_real_provider_keys(tmp_path: Path) -> None:
    """真实示例 eval 在清空外部 provider key 后仍调用 fake model 并通过。"""

    state_dir = tmp_path / "fake-eval-state"
    state_dir.mkdir()
    run_migrations(_dsn(state_dir / "eval.db"))
    environment = os.environ.copy()
    for name in (
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
    ):
        environment.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            str(SERVICE_APP / "scripts" / "run_example_evals.py"),
            "--state-dir",
            str(state_dir),
            "--agent",
            "examples.ticket_triage",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "agent=examples.ticket_triage status=completed cases=2" in result.stdout
    trace_payloads = [
        json.loads(line)
        for line in (state_dir / "traces.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    model_usage = [
        item for item in trace_payloads if item.get("event_type") == "model.usage.updated"
    ]
    assert model_usage
    assert all(item["payload"]["usage"]["provider"] == "fake" for item in model_usage)


def test_nested_default_shaped_eval_state_replays_with_one_explicit_manifest(
    tmp_path: Path,
) -> None:
    """显式 `.agent-harness/eval` bundle 连续运行时不得把 manifest 折叠到父目录。"""

    state_dir = tmp_path / ".agent-harness" / "eval"
    state_dir.mkdir(parents=True)
    run_migrations(_dsn(state_dir / "eval.db"))
    command_line = [
        sys.executable,
        str(SERVICE_APP / "scripts" / "run_example_evals.py"),
        "--state-dir",
        str(state_dir),
        "--agent",
        "examples.ticket_triage",
    ]

    results = [
        subprocess.run(
            command_line,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        for _ in range(2)
    ]

    assert [result.returncode for result in results] == [0, 0], [
        result.stderr for result in results
    ]
    assert all("example-eval: status=ok failures=0" in result.stdout for result in results)
    manifest_path = state_dir / "local-state-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert not (state_dir.parent / "local-state-manifest.json").exists()
    assert {Path(item["path"]) for item in manifest["files"]} == {
        (state_dir / "scores.jsonl").resolve(),
        (state_dir / "traces.jsonl").resolve(),
    }
    assert {item["state_dir"] for item in manifest["files"]} == {str(state_dir.resolve())}


def test_example_eval_rejects_legacy_score_before_run_or_event_side_effects(
    tmp_path: Path,
) -> None:
    """脚本显式 inventory 包含 score；legacy score 不能等到首个 run 后才失败。"""

    state_dir = tmp_path / "eval-state"
    state_dir.mkdir()
    database = state_dir / "eval.db"
    run_migrations(_dsn(database))
    scores_path = state_dir / "scores.jsonl"
    original = b'{"run_id":"legacy-run","value":1}\n'
    scores_path.write_bytes(original)

    result = subprocess.run(
        [
            sys.executable,
            str(SERVICE_APP / "scripts" / "run_example_evals.py"),
            "--state-dir",
            str(state_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "local_state.migration_required" in result.stderr
    assert result.stdout == ""
    with sqlite3.connect(database) as connection:
        assert connection.execute("select count(*) from agent_runs").fetchone() == (0,)
        assert connection.execute("select count(*) from run_trace_bindings").fetchone() == (0,)
        assert connection.execute("select count(*) from audit_logs").fetchone() == (0,)
    assert scores_path.read_bytes() == original
    assert not (state_dir / "traces.jsonl").exists()


@pytest.mark.asyncio
async def test_0008_downgrade_only_allows_empty_disposable_data(tmp_path: Path) -> None:
    """0008 空数据可逆；任一 resolution/claim 数据存在时必须拒绝。"""

    empty_dsn = _dsn(tmp_path / "empty-downgrade.db")
    run_migrations(empty_dsn)
    await asyncio.to_thread(
        command.downgrade,
        _downgrade_config(empty_dsn),
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
                request_id="req-example-eval-resolution",
            )
            await uow.commit()
    finally:
        await components.close()

    with pytest.raises(RuntimeError, match="0016 shared budget evidence exists"):
        await asyncio.to_thread(
            command.downgrade,
            _downgrade_config(_dsn(db_path)),
            "0007_eval_gate_trace_loop",
        )
    assert (
        await asyncio.to_thread(get_current_revision, _dsn(db_path))
        == "0017_model_route_chain_state"
    )
