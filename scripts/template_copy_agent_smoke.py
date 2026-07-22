"""验证仓库外模板的 scaffold、CLI 运行与人工批准 eval 表面。"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


def run_agent_surface_smoke(
    *,
    copied: Path,
    state: Path,
    env: dict[str, str],
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """在复制模板内完成生成、发现、运行与 eval 审核闭环。"""

    scaffold_result = run_command(
        ["uv", "run", "agent-harness", "scaffold", "agent", "generated.smoke"],
        cwd=copied,
        env=env,
    )
    generated = copied / "agents" / "generated" / "smoke"
    if "created: generated/smoke" not in scaffold_result.stdout or not generated.is_dir():
        raise RuntimeError(
            f"copied scaffold did not discover <copied-root>/agents:\n{scaffold_result.stdout}"
        )
    if (copied / "templates" / "service-app" / "agents").exists():
        raise RuntimeError("copied scaffold created a nested source-workspace path")
    scaffold_dsn = f"sqlite+aiosqlite:///{state / 'scaffold.db'}"
    profiles_dir = copied / "configs" / "profiles"
    agents_dir = copied / "agents"
    run_command(
        [
            "uv",
            "run",
            "python",
            "app/migrate.py",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--storage-dsn",
            scaffold_dsn,
        ],
        cwd=copied,
        env=env,
    )
    list_result = run_command(
        [
            "uv",
            "run",
            "agent-harness",
            "agents",
            "list",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--agents-dir",
            str(agents_dir),
            "--storage-dsn",
            scaffold_dsn,
        ],
        cwd=copied,
        env=env,
    )
    if "generated.smoke" not in list_result.stdout:
        raise RuntimeError(f"copied scaffold agent is absent from CLI list:\n{list_result.stdout}")
    run_result = run_command(
        [
            "uv",
            "run",
            "agent-harness",
            "run",
            "generated.smoke",
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--agents-dir",
            str(agents_dir),
            "--storage-dsn",
            scaffold_dsn,
            "--events-path",
            str(state / "scaffold-events.jsonl"),
            "--prompt",
            "复制模板 scaffold runtime 验证",
        ],
        cwd=copied,
        env=env,
    )
    if "status: completed" not in run_result.stdout:
        raise RuntimeError(f"copied scaffold agent did not complete:\n{run_result.stdout}")

    dataset_dir = generated / "evals"
    scores_path = state / "scaffold-scores.jsonl"
    draft_result = run_command(
        [
            "uv",
            "run",
            "agent-harness",
            "eval",
            "draft",
            "generated.smoke",
            "--dataset-dir",
            str(dataset_dir),
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--storage-dsn",
            scaffold_dsn,
            "--scores-path",
            str(scores_path),
            "--trigger",
            "manual",
            "--prompt",
            "复制模板 eval 验证",
            "--output",
            "scaffold-ready",
            "--expected",
            "scaffold-ready",
        ],
        cwd=copied,
        env=env,
    )
    case_id = next(
        (
            line.removeprefix("case_id: ")
            for line in draft_result.stdout.splitlines()
            if line.startswith("case_id: ")
        ),
        None,
    )
    if case_id is None:
        raise RuntimeError(f"copied eval draft did not return case_id:\n{draft_result.stdout}")
    run_command(
        [
            "uv",
            "run",
            "agent-harness",
            "eval",
            "approve",
            case_id,
            "--dataset-dir",
            str(dataset_dir),
            "--profile",
            "local",
            "--profiles-dir",
            str(profiles_dir),
            "--storage-dsn",
            scaffold_dsn,
            "--scores-path",
            str(scores_path),
            "--reviewer",
            "copy-smoke-reviewer",
            "--reason",
            "复制模板人工审核边界验证",
        ],
        cwd=copied,
        env=env,
    )
    eval_result = run_command(
        [
            "uv",
            "run",
            "agent-harness",
            "eval",
            "run",
            "--dataset-dir",
            str(dataset_dir),
            "--scores-path",
            str(scores_path),
            "--agent-id",
            "generated.smoke",
        ],
        cwd=copied,
        env=env,
    )
    if '"passed": 1' not in eval_result.stdout:
        raise RuntimeError(f"copied approved eval did not pass:\n{eval_result.stdout}")


__all__ = ["run_agent_surface_smoke"]
