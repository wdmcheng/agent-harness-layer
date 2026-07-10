"""验证复制后的 service profile PostgreSQL、Redis、repository 与 worker seam。"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from agent_harness.config import load_settings
from agent_harness.storage import (
    ApprovalCreate,
    EvalDatasetSplitCreate,
    EvalExperimentCreate,
    ExperimentStorageConflict,
    HarnessAcceptanceCreate,
    SQLAlchemyStorage,
    ToolInvocationCreate,
    run_migrations,
)
from agent_harness.storage.access_repositories import ApprovalResolutionRepositoryConflict
from agent_harness.storage.diagnostics import migration_revision, redis_status
from agent_harness.storage.repositories import RunCreate, SessionCreate

APP_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = APP_ROOT / "docker-compose.yml"
PROFILES = APP_ROOT / "configs" / "profiles"
PROJECT_NAME = os.environ.get("SERVICE_APP_COMPOSE_PROJECT", "agent-harness-service-app")


def run_compose_up() -> None:
    """只管理本模板 compose project，并等待 PostgreSQL/Redis healthcheck。"""

    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "-p",
            PROJECT_NAME,
            "--profile",
            "service",
            "up",
            "-d",
            "--wait",
        ],
        cwd=APP_ROOT,
        env=os.environ.copy(),
        check=True,
    )


async def repository_probe(dsn: str) -> str:
    """穿过真实 PostgreSQL repository/UoW 创建 run，而非只检查端口。"""

    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            tenant = await uow.tenants.ensure("default")
            session = await uow.sessions.create(
                SessionCreate(
                    tenant_id=tenant.id,
                    user_id="service-template-smoke",
                    agent_id="examples.basic",
                )
            )
            run = await uow.runs.create(
                RunCreate(
                    tenant_id=tenant.id,
                    session_id=session.id,
                    agent_id="examples.basic",
                    idempotency_key=None,
                    input={"smoke": "service-template"},
                )
            )
            await uow.commit()
        return run.id
    finally:
        await storage.dispose()


async def eval_experiment_probe(dsn: str) -> str:
    """验证模板复制后仍可使用 Phase 12.5 PostgreSQL repositories。"""

    suffix = str(uuid4())
    split_id = f"split-smoke-{suffix}"
    key = f"eval-smoke-{suffix}"
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("default")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id=split_id,
                    tenant_id="default",
                    agent_id="examples.basic",
                    dataset="service-smoke",
                    request_id=f"request-{suffix}",
                    tags=["tool_selection"],
                    strategy="deterministic_multilabel_v1",
                    optimization_ratio=0.8,
                    holdout_ratio=0.2,
                    case_tags={
                        f"case-opt-{suffix}": ["tool_selection"],
                        f"case-holdout-{suffix}": ["tool_selection"],
                    },
                    optimization_case_ids=[f"case-opt-{suffix}"],
                    holdout_case_ids=[f"case-holdout-{suffix}"],
                    regression_case_ids=[],
                )
            )
            create = EvalExperimentCreate(
                tenant_id="default",
                idempotency_key=key,
                request_hash="a" * 64,
                request_id=f"request-{suffix}",
                agent_id="examples.basic",
                dataset="service-smoke",
                split_id=split_id,
                evaluator_profile={"name": "service-smoke", "version": "1"},
                metric_versions={"exact_match": "1"},
                baseline_harness={"version_id": f"baseline-{suffix}"},
                candidate_harness={"version_id": f"candidate-{suffix}"},
            )
            experiment = await uow.eval_experiments.create(create)
            replay = await uow.eval_experiments.create(create)
            if replay.experiment_id != experiment.experiment_id:
                raise RuntimeError("eval experiment idempotent replay created another row")
            await uow.eval_experiments.update_results(
                tenant_id="default",
                experiment_id=experiment.experiment_id,
                status="completed",
                baseline_run_ref=f"eval-run://{suffix}/baseline",
                candidate_run_ref=f"eval-run://{suffix}/candidate",
                score_summaries={"baseline": {"score": 0.5}, "candidate": {"score": 0.75}},
                comparison={"acceptance_recommendation": "accept"},
                local_refs=[f"artifact://service-smoke/{suffix}/comparison"],
                provider_statuses=[],
            )
            decision = HarnessAcceptanceCreate(
                tenant_id="default",
                experiment_id=experiment.experiment_id,
                decision_request_hash="b" * 64,
                reviewer_id="service-template-smoke",
                reason="PostgreSQL repository parity probe",
                decision="accepted",
                accepted_harness_version=f"candidate-{suffix}",
                production_binding={"version_id": f"candidate-{suffix}"},
                policy_decision={"decision": "allow"},
                audit_ref=f"audit://service-smoke/{suffix}",
                evidence_refs=[f"artifact://service-smoke/{suffix}/comparison"],
            )
            accepted = await uow.harness_acceptance_records.create(decision)
            replayed_decision = await uow.harness_acceptance_records.create(decision)
            if replayed_decision.acceptance_id != accepted.acceptance_id:
                raise RuntimeError("eval acceptance replay created another row")
            await uow.commit()
        try:
            async with storage.uow() as uow:
                await uow.eval_experiments.create(
                    create.model_copy(update={"request_hash": "c" * 64})
                )
        except ExperimentStorageConflict as exc:
            if exc.code != "eval.experiment.idempotency_conflict":
                raise
        else:  # pragma: no cover - PostgreSQL parity failure only
            raise RuntimeError("eval experiment idempotency conflict was not rejected")
        return f"experiment={experiment.experiment_id} acceptance={accepted.acceptance_id}"
    finally:
        await storage.dispose()


async def approval_claim_probe(dsn: str, run_id: str) -> str:
    """证明 PostgreSQL 条件仲裁和 nullable unique approval claim 真正生效。"""

    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            approval = await uow.approvals.create(
                ApprovalCreate(
                    tenant_id="default",
                    run_id=run_id,
                    agent_id="examples.basic",
                    action="shell.execute",
                    resource="shell:workspace",
                    reason="service smoke approval claim",
                    requested_by="service-template-smoke",
                    metadata={"arguments_hash": "0" * 64},
                )
            )
            await uow.commit()
        async with storage.uow() as uow:
            lease = await uow.approvals.claim_resolution(
                approval_id=approval.approval_id,
                run_id=run_id,
                tenant_id="default",
            )
            await uow.commit()
        try:
            async with storage.uow() as uow:
                await uow.approvals.deny_waiting(
                    approval_id=approval.approval_id,
                    run_id=run_id,
                    tenant_id="default",
                    resolved_by="service-template-smoke",
                )
        except ApprovalResolutionRepositoryConflict as exc:
            if exc.code != "approval.resolution_in_progress":
                raise
        else:  # pragma: no cover - PostgreSQL 条件更新失效时才会进入
            raise RuntimeError("approval deny unexpectedly bypassed the claimed lease")

        claim = ToolInvocationCreate(
            tenant_id="default",
            agent_id="examples.basic",
            run_id=run_id,
            tool_name="shell.execute",
            args_ref="artifact://service-smoke-args",
            approval_id=approval.approval_id,
            arguments_hash="0" * 64,
            execution_state="executing",
            status="executing",
            metadata={"lease_id": lease.lease_id},
        )
        async with storage.uow() as uow:
            await uow.tool_invocations.create(claim)
            await uow.commit()
        try:
            async with storage.uow() as uow:
                await uow.tool_invocations.create(claim)
                await uow.commit()
        except IntegrityError:
            pass
        else:  # pragma: no cover - unique 约束失效时才会进入
            raise RuntimeError("duplicate approval tool claim was not rejected")
        return f"approval={approval.approval_id} lease={lease.lease_id} unique=ok"
    finally:
        await storage.dispose()


def worker_probe(dsn: str) -> str:
    """用模板自身 worker 入口执行一次 service run。"""

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.workers.runtime_worker",
            "--once",
            "--profile",
            "service",
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            dsn,
            "--events-path",
            str(APP_ROOT / ".agent-harness" / "service-worker-events.jsonl"),
        ],
        cwd=APP_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip().removeprefix("runtime-worker: run_id=")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    """输出 reviewer 可复核的依赖、migration 与行为证据。"""

    args = parse_args()
    settings = load_settings(profile="service", profiles_dir=PROFILES)
    if settings.storage.dsn is None:
        raise RuntimeError("service profile requires storage.dsn")

    run_compose_up()
    run_migrations(settings.storage.dsn)
    revision = migration_revision(settings)
    redis_ok, redis_message = redis_status(settings, timeout_seconds=2.0)
    if revision != "0009_eval_experiment_loop":
        raise RuntimeError(f"PostgreSQL migration is not at the expected head: {revision}")
    if not redis_ok:
        raise RuntimeError(f"Redis check failed: {redis_message}")

    repository_run = "(migrate-only)"
    approval_claim = "(migrate-only)"
    eval_experiment = "(migrate-only)"
    worker_run = "(migrate-only)"
    if not args.migrate_only:
        repository_run = asyncio.run(repository_probe(settings.storage.dsn))
        eval_experiment = asyncio.run(eval_experiment_probe(settings.storage.dsn))
        approval_claim = asyncio.run(approval_claim_probe(settings.storage.dsn, repository_run))
        worker_run = worker_probe(settings.storage.dsn)

    print(f"smoke-service: migration={revision}")
    print(f"smoke-service: redis={redis_message}")
    print(f"smoke-service: repository_run={repository_run}")
    print(f"smoke-service: eval_experiment={eval_experiment}")
    print(f"smoke-service: approval_claim={approval_claim}")
    print(f"smoke-service: worker_run={worker_run}")
    print("smoke-service: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
