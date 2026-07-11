"""Eval experiment SQLite migration、升级与降级合同。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn


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
            row[1] for row in connection.execute("pragma table_info(eval_experiments)").fetchall()
        }
        acceptance_columns = {
            row[1]
            for row in connection.execute(
                "pragma table_info(harness_acceptance_records)"
            ).fetchall()
        }
        experiment_indexes = connection.execute("pragma index_list(eval_experiments)").fetchall()
        experiment_schema = connection.execute(
            "select sql from sqlite_master where type='table' and name='eval_experiments'"
        ).fetchone()
        acceptance_schema = connection.execute(
            """
            select sql from sqlite_master
            where type='table' and name='harness_acceptance_records'
            """
        ).fetchone()

    assert get_current_revision(dsn) == "0012_service_runtime_execution_context"
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
        "execution_claim_id",
        "execution_claim_expires_at",
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


def test_0011_upgrades_existing_0009_experiment_without_rewriting_terminal_evidence(
    tmp_path: Path,
) -> None:
    from agent_harness.storage import get_current_revision, run_migrations
    from agent_harness.storage.migrations.runner import alembic_config

    db_path = tmp_path / "upgrade-existing-0009.db"
    dsn = sqlite_dsn(db_path)
    command.upgrade(alembic_config(dsn), "0009_eval_experiment_loop")
    with sqlite3.connect(db_path) as connection:
        connection.execute("insert into tenants (id, display_name) values ('tenant-a', 'A')")
        connection.execute(
            """
            insert into eval_dataset_splits (
                id, tenant_id, agent_id, dataset, request_id, tags_json, strategy,
                optimization_ratio, holdout_ratio, regression_policy_json,
                case_tags_json, optimization_case_ids_json, holdout_case_ids_json,
                regression_case_ids_json, optimization_case_count, holdout_case_count,
                regression_case_count, tag_distribution_json, rejected_counts_json,
                evidence_refs_json
            ) values (
                'split-legacy', 'tenant-a', 'examples.basic', 'default', 'request-legacy',
                '["tool_selection"]', 'deterministic_multilabel_v1', 0.8, 0.2, '{}',
                '{}', '["case-o"]', '["case-h"]', '[]', 1, 1, 0, '{}', '{}', '[]'
            )
            """
        )
        connection.execute(
            """
            insert into eval_experiments (
                id, tenant_id, idempotency_key, request_hash, request_id, agent_id,
                dataset, split_id, status, evaluator_profile_json, metric_versions_json,
                baseline_harness_json, candidate_harness_json, baseline_run_ref,
                candidate_run_ref, score_summaries_json, comparison_json,
                local_refs_json, provider_status_json, metadata_json
            ) values (
                'experiment-legacy', 'tenant-a', 'legacy-key', :request_hash,
                'request-legacy', 'examples.basic', 'default', 'split-legacy', 'completed',
                '{}', '{}', '{}', null, 'eval-run://legacy', null,
                '{"baseline":{"score":1.0}}', '{}', '["artifact://legacy"]', '[]', '{}'
            )
            """,
            {"request_hash": "a" * 64},
        )
        connection.execute(
            """
            insert into eval_experiments (
                id, tenant_id, idempotency_key, request_hash, request_id, agent_id,
                dataset, split_id, status, evaluator_profile_json, metric_versions_json,
                baseline_harness_json, candidate_harness_json, baseline_run_ref,
                candidate_run_ref, score_summaries_json, comparison_json,
                local_refs_json, provider_status_json, metadata_json
            ) values (
                'experiment-legacy-created', 'tenant-a', 'legacy-created-key', :request_hash,
                'request-legacy-created', 'examples.basic', 'default', 'split-legacy',
                'created', '{}', '{}', '{}', null, null, null, '{}', '{}', '[]', '[]', '{}'
            )
            """,
            {"request_hash": "b" * 64},
        )
        connection.commit()

    assert get_current_revision(dsn) == "0009_eval_experiment_loop"
    run_migrations(dsn)

    assert get_current_revision(dsn) == "0012_service_runtime_execution_context"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            select status, score_summaries_json, local_refs_json,
                   execution_claim_id, execution_claim_expires_at
            from eval_experiments where id = 'experiment-legacy'
            """
        ).fetchone()
    assert row == (
        "completed",
        '{"baseline":{"score":1.0}}',
        '["artifact://legacy"]',
        None,
        None,
    )
    with sqlite3.connect(db_path) as connection:
        legacy_created = connection.execute(
            """
            select status, execution_claim_id, execution_claim_expires_at
            from eval_experiments where id = 'experiment-legacy-created'
            """
        ).fetchone()
    assert legacy_created == ("needs_review", None, None)


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

    with pytest.raises(RuntimeError, match="0011 downgrade refused"):
        command.downgrade(alembic_config(used_dsn), "0008_agent_execution_approval_claims")
    # 0012 没有 durable execution evidence 时可先安全回退；随后 0011 在 eval evidence 处拒绝。
    assert get_current_revision(used_dsn) == "0011_eval_experiment_legacy_created_review"
    with sqlite3.connect(used_db) as connection:
        assert connection.execute("select count(*) from eval_dataset_splits").fetchone() == (1,)
