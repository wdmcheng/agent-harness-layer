"""Eval case 生命周期与评分文件的 Typer 命令注册。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from agent_harness import cli_shared
from agent_harness.cli_eval import approve_eval_case, draft_eval_case
from agent_harness.evals import EvalRunner, ScoreSink


def eval_draft(
    agent_id: str,
    dataset_dir: Annotated[Path, typer.Option("--dataset-dir")] = Path(
        "templates/service-app/eval-cases"
    ),
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    scores_path: Annotated[Path, typer.Option("--scores-path")] = Path(
        ".agent-harness/eval/scores.jsonl"
    ),
    run_id: Annotated[str | None, typer.Option("--run-id")] = None,
    trace_id: Annotated[str | None, typer.Option("--trace-id")] = None,
    trigger: Annotated[str, typer.Option("--trigger")] = "failed_run",
    prompt: Annotated[str, typer.Option("--prompt")] = "",
    output: Annotated[str | None, typer.Option("--output")] = None,
    expected: Annotated[str | None, typer.Option("--expected")] = None,
    score: Annotated[list[str] | None, typer.Option("--score")] = None,
    score_threshold: Annotated[float | None, typer.Option("--score-threshold")] = None,
) -> None:
    """从 CLI 输入生成 repository-backed draft eval case，不写 approved dataset。"""

    record = draft_eval_case(
        agent_id=agent_id,
        dataset_dir=dataset_dir,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        scores_path=scores_path,
        run_id=run_id,
        trace_id=trace_id,
        trigger=trigger,
        prompt=prompt,
        output=output,
        expected=expected,
        score=score,
        score_threshold=score_threshold,
    )
    typer.echo(f"case_id: {record.case_id}")
    typer.echo("status: draft")


def eval_approve(
    case_id: str,
    dataset_dir: Annotated[Path, typer.Option("--dataset-dir")] = Path(
        "templates/service-app/eval-cases"
    ),
    profile: Annotated[str, typer.Option("--profile")] = "local",
    profiles_dir: Annotated[Path | None, typer.Option("--profiles-dir")] = None,
    storage_dsn: Annotated[str | None, typer.Option("--storage-dsn")] = None,
    scores_path: Annotated[Path, typer.Option("--scores-path")] = Path(
        ".agent-harness/eval/scores.jsonl"
    ),
    reviewer: Annotated[str, typer.Option("--reviewer")] = "local-reviewer",
    reason: Annotated[str, typer.Option("--reason")] = "approved via CLI",
    dataset: Annotated[str, typer.Option("--dataset")] = "default",
) -> None:
    """把 draft case 人工确认到 approved dataset，并写 repository/audit。"""

    record = approve_eval_case(
        case_id=case_id,
        dataset_dir=dataset_dir,
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        scores_path=scores_path,
        reviewer=reviewer,
        reason=reason,
        dataset=dataset,
    )
    typer.echo(f"case_id: {record.case_id}")
    typer.echo("status: approved")


def eval_list(
    dataset_dir: Annotated[Path, typer.Option("--dataset-dir")] = Path(
        "templates/service-app/eval-cases"
    ),
    status: Annotated[str, typer.Option("--status")] = "approved",
    agent_id: Annotated[str | None, typer.Option("--agent-id")] = None,
) -> None:
    """列出 draft 或 approved eval case 文件摘要。"""

    from agent_harness.evals import ReviewDatasetAdapter

    adapter = ReviewDatasetAdapter(
        drafts_dir=dataset_dir / "drafts",
        approved_dir=dataset_dir / "approved",
    )
    cases = (
        adapter.load_approved(agent_id=agent_id)
        if status == "approved"
        else [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((dataset_dir / "drafts").glob("*.json"))
        ]
    )
    for case in cases:
        if agent_id is not None and case.get("agent_id") != agent_id:
            continue
        typer.echo(json.dumps(case, ensure_ascii=False, sort_keys=True))


def eval_run(
    dataset_dir: Annotated[Path, typer.Option("--dataset-dir")] = Path(
        "templates/service-app/eval-cases"
    ),
    scores_path: Annotated[Path, typer.Option("--scores-path")] = Path(
        ".agent-harness/eval/scores.jsonl"
    ),
    agent_id: Annotated[str, typer.Option("--agent-id")] = "examples.basic",
    tenant_id: Annotated[str, typer.Option("--tenant-id")] = "default",
) -> None:
    """运行 approved eval cases；draft 只统计不执行。"""

    cli_shared.require_local_state_ready_or_exit(score_paths=(scores_path,))
    runner = EvalRunner(score_sink=ScoreSink(local_path=scores_path))
    result = asyncio.run(
        runner.run_file_dataset(
            dataset_dir=dataset_dir,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
    )
    typer.echo(f"eval_run_id: {result.eval_run_id}")
    typer.echo(f"status: {result.status}")
    typer.echo(f"case_count: {result.case_count}")
    typer.echo(f"skipped_drafts: {result.skipped_drafts}")
    typer.echo(f"score_summary: {json.dumps(result.score_summary, ensure_ascii=False)}")


def eval_scores(
    scores_path: Annotated[Path, typer.Option("--scores-path")] = Path(
        ".agent-harness/eval/scores.jsonl"
    ),
) -> None:
    """输出本地 score JSONL。"""

    cli_shared.require_local_state_ready_or_exit(score_paths=(scores_path,))
    if not scores_path.exists():
        return
    typer.echo(scores_path.read_text(encoding="utf-8"), nl=False)


def register_eval_commands(eval_app: typer.Typer) -> None:
    """把 eval 命令组注册到调用方持有的 Typer app。"""

    eval_app.command("draft")(eval_draft)
    eval_app.command("approve")(eval_approve)
    eval_app.command("list")(eval_list)
    eval_app.command("run")(eval_run)
    eval_app.command("scores")(eval_scores)


__all__ = ["register_eval_commands"]
