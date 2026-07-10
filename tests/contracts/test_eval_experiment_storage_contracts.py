"""Phase 12.5 eval experiment migration 与 repository 合同测试。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from pydantic import ValidationError
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn

from agent_harness.storage import (
    EvalDatasetSplitCreate,
    EvalExperimentCreate,
    HarnessAcceptanceCreate,
)


def _split_create() -> EvalDatasetSplitCreate:
    return EvalDatasetSplitCreate(
        split_id="split-1",
        tenant_id="tenant-a",
        agent_id="examples.basic",
        dataset="default",
        request_id="request-1",
        tags=["retrieval_quality", "tool_selection"],
        strategy="deterministic_multilabel_v1",
        optimization_ratio=0.8,
        holdout_ratio=0.2,
        regression_policy={"case_ids": ["case-r"]},
        case_tags={
            "case-o": ["retrieval_quality"],
            "case-h": ["retrieval_quality"],
            "case-r": ["policy_approval"],
        },
        optimization_case_ids=["case-o"],
        holdout_case_ids=["case-h"],
        regression_case_ids=["case-r"],
        tag_distribution={"retrieval_quality": {"optimization": 1, "holdout": 1}},
        rejected_counts={"draft": 2},
        evidence_refs=["artifact://split-1"],
    )


def _experiment_create(*, request_hash: str = "a" * 64) -> EvalExperimentCreate:
    return EvalExperimentCreate(
        tenant_id="tenant-a",
        idempotency_key="experiment-key",
        request_hash=request_hash,
        request_id="request-1",
        agent_id="examples.basic",
        dataset="default",
        split_id="split-1",
        evaluator_profile={"name": "exact-match", "version": "1"},
        metric_versions={"exact_match": "1"},
        baseline_harness={"version": "baseline-v1"},
        candidate_harness={"version": "candidate-v2"},
    )


def _acceptance_create(*, request_hash: str = "b" * 64) -> HarnessAcceptanceCreate:
    return HarnessAcceptanceCreate(
        tenant_id="tenant-a",
        experiment_id="experiment-1",
        decision_request_hash=request_hash,
        reviewer_id="reviewer-1",
        reason="holdout and regression evidence reviewed",
        decision="accepted",
        accepted_harness_version="candidate-v2",
        production_binding={"agent_id": "examples.basic", "version": "candidate-v2"},
        policy_decision={"decision": "allow", "reason": "manual review allowed"},
        audit_ref="audit://acceptance-1",
        evidence_refs=["artifact://comparison-1"],
    )


def test_0009_migration_creates_phase_12_5_schema(tmp_path: Path) -> None:
    from agent_harness.storage import get_current_revision, run_migrations

    db_path = tmp_path / "eval-experiment-schema.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        experiment_columns = {
            row[1]
            for row in connection.execute("pragma table_info(eval_experiments)").fetchall()
        }
        acceptance_columns = {
            row[1]
            for row in connection.execute(
                "pragma table_info(harness_acceptance_records)"
            ).fetchall()
        }
        experiment_indexes = connection.execute(
            "pragma index_list(eval_experiments)"
        ).fetchall()
        experiment_schema = connection.execute(
            "select sql from sqlite_master where type='table' and name='eval_experiments'"
        ).fetchone()
        acceptance_schema = connection.execute(
            """
            select sql from sqlite_master
            where type='table' and name='harness_acceptance_records'
            """
        ).fetchone()

    assert get_current_revision(dsn) == "0009_eval_experiment_loop"
    assert {
        "eval_dataset_splits",
        "eval_experiments",
        "harness_acceptance_records",
    } <= tables
    assert {
        "idempotency_key",
        "request_hash",
        "evaluator_profile_json",
        "metric_versions_json",
        "baseline_harness_json",
        "candidate_harness_json",
        "comparison_json",
        "local_refs_json",
        "provider_status_json",
    } <= experiment_columns
    assert {
        "decision_request_hash",
        "reviewer_id",
        "decision",
        "accepted_harness_version",
        "production_binding_json",
        "policy_decision_json",
        "audit_ref",
    } <= acceptance_columns
    assert any(row[2] == 1 for row in experiment_indexes)
    assert experiment_schema is not None
    assert "uq_eval_experiments_tenant_idempotency" in experiment_schema[0]
    assert "fk_eval_experiments_tenant_split" in experiment_schema[0]
    assert acceptance_schema is not None
    assert "fk_harness_acceptance_tenant_experiment" in acceptance_schema[0]
    assert "uq_harness_acceptance_experiment" in acceptance_schema[0]


@pytest.mark.asyncio
async def test_phase_12_5_repositories_are_tenant_scoped_and_idempotent(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import (
        ExperimentStorageConflict,
        ExperimentStorageNotFound,
        SQLAlchemyStorage,
        run_migrations,
    )

    dsn = sqlite_dsn(tmp_path / "eval-experiment-repository.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            split = await uow.eval_dataset_splits.create(_split_create())
            split_replay = await uow.eval_dataset_splits.create(
                _split_create().model_copy(update={"request_id": "request-retry"})
            )
            experiment = await uow.eval_experiments.create(_experiment_create())
            replay = await uow.eval_experiments.create(
                _experiment_create().model_copy(update={"request_id": "request-retry"})
            )
            await uow.commit()

        assert split.split_id == "split-1"
        assert split_replay.split_id == split.split_id
        assert experiment.experiment_id == replay.experiment_id
        assert experiment.request_hash == "a" * 64

        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-b", "split-1") is None
            assert await uow.eval_experiments.get("tenant-b", experiment.experiment_id) is None
            with pytest.raises(ExperimentStorageConflict) as conflict:
                await uow.eval_experiments.create(_experiment_create(request_hash="c" * 64))
        assert conflict.value.code == "eval.experiment.idempotency_conflict"

        async with storage.uow() as uow:
            cross_tenant = _experiment_create().model_copy(
                update={"tenant_id": "tenant-b", "idempotency_key": "tenant-b-key"}
            )
            with pytest.raises(ExperimentStorageNotFound) as hidden_split:
                await uow.eval_experiments.create(cross_tenant)
        assert hidden_split.value.code == "eval.experiment.split_not_found"

        async with storage.uow() as uow:
            wrong_agent = _experiment_create().model_copy(
                update={"agent_id": "examples.other", "idempotency_key": "wrong-agent-key"}
            )
            with pytest.raises(ExperimentStorageNotFound):
                await uow.eval_experiments.create(wrong_agent)

        async with storage.uow() as uow:
            changed_body = _experiment_create().model_copy(
                update={"candidate_harness": {"version": "candidate-v3"}}
            )
            with pytest.raises(ExperimentStorageConflict):
                await uow.eval_experiments.create(changed_body)
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_phase_12_5_result_update_and_acceptance_decision_are_immutable(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import (
        ExperimentStorageConflict,
        ExperimentStorageNotFound,
        SQLAlchemyStorage,
        run_migrations,
    )

    dsn = sqlite_dsn(tmp_path / "eval-experiment-results.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            await uow.eval_dataset_splits.create(_split_create())
            experiment = await uow.eval_experiments.create(_experiment_create())
            completed = await uow.eval_experiments.update_results(
                tenant_id="tenant-a",
                experiment_id=experiment.experiment_id,
                status="completed",
                baseline_run_ref="eval-run://baseline",
                candidate_run_ref="eval-run://candidate",
                score_summaries={
                    "baseline": {"exact_match": 0.6},
                    "candidate": {"exact_match": 0.8},
                },
                comparison={"recommendation": "accept"},
                local_refs=["artifact://comparison-1"],
                provider_statuses=[{"provider": "optional", "status": "degraded"}],
            )
            acceptance_data = _acceptance_create()
            acceptance_data.experiment_id = experiment.experiment_id
            decision = await uow.harness_acceptance_records.create(acceptance_data)
            replay = await uow.harness_acceptance_records.create(acceptance_data)
            await uow.commit()

        assert completed.status == "completed"
        assert completed.provider_statuses[0]["status"] == "degraded"
        assert decision.acceptance_id == replay.acceptance_id

        async with storage.uow() as uow:
            replay_with_new_evidence = _acceptance_create()
            replay_with_new_evidence.experiment_id = experiment.experiment_id
            replay_with_new_evidence.audit_ref = "audit://retry-is-correlation"
            replay_with_new_evidence.evidence_refs = ["artifact://retry-derived"]
            replayed = await uow.harness_acceptance_records.create(
                replay_with_new_evidence
            )
        assert replayed.acceptance_id == decision.acceptance_id

        async with storage.uow() as uow:
            changed = _acceptance_create(request_hash="d" * 64)
            changed.experiment_id = experiment.experiment_id
            with pytest.raises(ExperimentStorageConflict) as conflict:
                await uow.harness_acceptance_records.create(changed)
        assert conflict.value.code == "eval.experiment.decision_conflict"

        async with storage.uow() as uow:
            changed_reviewer = _acceptance_create()
            changed_reviewer.experiment_id = experiment.experiment_id
            changed_reviewer.reviewer_id = "reviewer-2"
            with pytest.raises(ExperimentStorageConflict):
                await uow.harness_acceptance_records.create(changed_reviewer)
            assert (
                await uow.harness_acceptance_records.get_for_experiment(
                    "tenant-b", experiment.experiment_id
                )
                is None
            )

            cross_tenant = _acceptance_create().model_copy(
                update={
                    "tenant_id": "tenant-b",
                    "experiment_id": experiment.experiment_id,
                }
            )
            with pytest.raises(ExperimentStorageNotFound) as hidden_experiment:
                await uow.harness_acceptance_records.create(cross_tenant)
        assert hidden_experiment.value.code == "eval.experiment.not_found"
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_phase_12_5_unit_of_work_rolls_back_without_commit(tmp_path: Path) -> None:
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "eval-experiment-rollback.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(_split_create())

        async with storage.uow() as uow:
            assert await uow.eval_dataset_splits.get("tenant-a", "split-1") is None
    finally:
        await storage.dispose()


def test_acceptance_dto_rejects_impossible_production_bindings() -> None:
    with pytest.raises(ValidationError):
        HarnessAcceptanceCreate(
            tenant_id="tenant-a",
            experiment_id="experiment-1",
            decision_request_hash="b" * 64,
            reviewer_id="reviewer-1",
            reason="missing accepted binding",
            decision="accepted",
            policy_decision={"decision": "allow"},
            audit_ref="audit://acceptance-1",
        )

    with pytest.raises(ValidationError):
        HarnessAcceptanceCreate(
            tenant_id="tenant-a",
            experiment_id="experiment-1",
            decision_request_hash="b" * 64,
            reviewer_id="reviewer-1",
            reason="rejected must not bind",
            decision="rejected",
            accepted_harness_version="candidate-v2",
            production_binding={"version": "candidate-v2"},
            policy_decision={"decision": "allow"},
            audit_ref="audit://acceptance-1",
        )


def test_0009_downgrade_is_empty_only_and_preserves_nonempty_evidence(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import get_current_revision, run_migrations
    from agent_harness.storage.migrations.runner import alembic_config

    empty_dsn = sqlite_dsn(tmp_path / "empty-downgrade.db")
    run_migrations(empty_dsn)
    command.downgrade(alembic_config(empty_dsn), "0008_agent_execution_approval_claims")
    assert get_current_revision(empty_dsn) == "0008_agent_execution_approval_claims"

    used_db = tmp_path / "used-downgrade.db"
    used_dsn = sqlite_dsn(used_db)
    run_migrations(used_dsn)
    with sqlite3.connect(used_db) as connection:
        connection.execute("insert into tenants (id, display_name) values ('tenant-a', 'A')")
        connection.execute(
            """
            insert into eval_dataset_splits (
                id, tenant_id, agent_id, dataset, request_id, tags_json, strategy,
                optimization_ratio, holdout_ratio, regression_policy_json,
                case_tags_json,
                optimization_case_ids_json, holdout_case_ids_json,
                regression_case_ids_json, optimization_case_count, holdout_case_count,
                regression_case_count, tag_distribution_json, rejected_counts_json,
                evidence_refs_json
            ) values (
                'split-1', 'tenant-a', 'examples.basic', 'default', 'request-1', '[]',
                'deterministic_multilabel_v1', 0.8, 0.2, '{}', '{}', '[]', '[]', '[]',
                0, 0, 0, '{}', '{}', '[]'
            )
            """
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="0009 downgrade refused"):
        command.downgrade(alembic_config(used_dsn), "0008_agent_execution_approval_claims")
    assert get_current_revision(used_dsn) == "0009_eval_experiment_loop"
    with sqlite3.connect(used_db) as connection:
        assert connection.execute("select count(*) from eval_dataset_splits").fetchone() == (1,)
