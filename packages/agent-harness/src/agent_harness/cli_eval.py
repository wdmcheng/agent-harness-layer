"""核心 CLI 的 eval draft/approve 应用服务装配。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from agent_harness.cli_shared import load_settings_or_exit
from agent_harness.config import HarnessSettings
from agent_harness.evals import EvalCaseFactory, EvalService, EvalTraceSource, ScoreSink
from agent_harness.storage import (
    EvalCaseRecord,
    SQLAlchemyStorage,
    run_migrations,
    storage_dsn_from_settings,
)


def draft_eval_case(
    *,
    agent_id: str,
    dataset_dir: Path,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
    scores_path: Path,
    run_id: str | None,
    trace_id: str | None,
    trigger: str,
    prompt: str,
    output: str | None,
    expected: str | None,
    score: list[str] | None,
    score_threshold: float | None,
) -> EvalCaseRecord:
    """从 CLI 参数创建 draft case，并在同步命令退出前释放 storage。"""

    settings, storage, service = _eval_service_from_cli(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        dataset_dir=dataset_dir,
        scores_path=scores_path,
    )

    async def _draft() -> EvalCaseRecord:
        try:
            return await service.draft_from_trace(
                EvalTraceSource(
                    tenant_id=settings.identity.default.tenant_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    trigger=trigger,
                    input={"prompt": prompt},
                    output=None if output is None else {"answer": output},
                    expected=None if expected is None else {"answer": expected},
                    scores=_parse_eval_scores(score),
                    source_refs=[],
                    artifact_refs=[],
                ),
                score_threshold=score_threshold,
            )
        finally:
            await storage.dispose()

    return asyncio.run(_draft())


def approve_eval_case(
    *,
    case_id: str,
    dataset_dir: Path,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
    scores_path: Path,
    reviewer: str,
    reason: str,
    dataset: str,
) -> EvalCaseRecord:
    """把 CLI 人工确认转换为 approved case，并返回稳定记录。"""

    settings, storage, service = _eval_service_from_cli(
        profile=profile,
        profiles_dir=profiles_dir,
        storage_dsn=storage_dsn,
        dataset_dir=dataset_dir,
        scores_path=scores_path,
    )
    actor = settings.identity.default.model_copy(update={"user_id": reviewer})

    async def _approve() -> EvalCaseRecord:
        try:
            result = await service.approve_case(
                actor=actor,
                case_id=case_id,
                reason=reason,
                dataset=dataset,
            )
            return result.case
        finally:
            await storage.dispose()

    return asyncio.run(_approve())


def _eval_service_from_cli(
    *,
    profile: str,
    profiles_dir: Path | None,
    storage_dsn: str | None,
    dataset_dir: Path,
    scores_path: Path,
) -> tuple[HarnessSettings, SQLAlchemyStorage, EvalService]:
    settings = load_settings_or_exit(profile, profiles_dir)
    resolved_dsn = storage_dsn or storage_dsn_from_settings(settings)
    run_migrations(resolved_dsn)
    storage = SQLAlchemyStorage.from_dsn(resolved_dsn)
    service = EvalService(
        storage=storage,
        factory=EvalCaseFactory(),
        score_sink=ScoreSink(local_path=scores_path),
        drafts_dir=dataset_dir / "drafts",
        approved_dir=dataset_dir / "approved",
    )
    return settings, storage, service


def _parse_eval_scores(score_items: list[str] | None) -> dict[str, float]:
    scores: dict[str, float] = {}
    for item in score_items or []:
        metric, separator, value = item.partition("=")
        if not metric or separator != "=":
            typer.echo(f"eval.invalid_score: {item}", err=True)
            raise typer.Exit(2)
        try:
            scores[metric] = float(value)
        except ValueError as exc:
            typer.echo(f"eval.invalid_score: {item}", err=True)
            raise typer.Exit(2) from exc
    return scores
