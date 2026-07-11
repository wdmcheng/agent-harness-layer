"""EVL-004 Typer 公共入口与 HTTP 等价 JSON 合同。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from tests.contracts.auth_policy_hitl_contract_helpers import sqlite_dsn, table_count
from tests.contracts.test_eval_experiment_api_contracts import SplitAwareEvaluator
from typer.testing import CliRunner

from agent_harness.cli import app


class CliBoundaryCaseRefsEvaluator(SplitAwareEvaluator):
    async def evaluate(self, **kwargs: Any):
        result = await super().evaluate(**kwargs)
        item = result.case_results[0]
        baseline = result.harness_version_id == self.baseline_id
        item.metric_scores = {"exact_match": 1.0 if baseline else 0.0}
        item.passed = baseline
        side = "baseline" if baseline else "candidate"
        item.evidence_refs = [f"artifact://cli-bounded-case/{side}/{index}" for index in range(60)]
        return result


def _manifest(seed: str):
    from agent_harness.evals import HarnessInputSource, HarnessVersionBuilder

    return HarnessVersionBuilder().build(
        {
            "prompt_instruction": HarnessInputSource(value={"prompt": seed}),
            "tool_descriptions": HarnessInputSource(value=[]),
            "agent_config": HarnessInputSource(value={"max_steps": 4}),
            "retrieval_config": HarnessInputSource(value={"top_k": 5}),
            "policy_defaults": HarnessInputSource(value={"network": "deny"}),
            "model_adapter_settings": HarnessInputSource(value={"adapter": "fake"}),
        }
    )


def _seed_approved_cases(*, storage: Any, baseline_id: str, candidate_id: str) -> None:
    from agent_harness.storage import EvalCaseCreate

    async def seed() -> None:
        async with storage.uow() as uow:
            await uow.tenants.ensure("default")
            for index in range(3):
                case = await uow.eval_cases.create(
                    EvalCaseCreate(
                        tenant_id="default",
                        agent_id="examples.basic",
                        name=f"cli-case-{index}",
                        payload={
                            "output": {"answer": index},
                            "expected": {"answer": index},
                        },
                        metadata={
                            "behavior_tags": ["tool_selection"],
                            "experiment_scores": {
                                baseline_id: {"exact_match": 0.5},
                                candidate_id: {"exact_match": 0.9},
                            },
                        },
                    )
                )
                await uow.eval_cases.approve(
                    case_id=case.case_id,
                    tenant_id="default",
                    approved_by="curator",
                    reason="safe CLI fixture",
                )
            await uow.commit()

    asyncio.run(seed())


def _invoke(runner: CliRunner, args: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_eval_experiment_cli_create_show_compare_accept_and_errors(tmp_path: Path) -> None:
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "eval-experiment-cli.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = _manifest("baseline")
    candidate = _manifest("candidate")
    _seed_approved_cases(
        storage=storage,
        baseline_id=baseline.version_id,
        candidate_id=candidate.version_id,
    )
    asyncio.run(storage.dispose())
    request_file = tmp_path / "experiment.json"
    request_file.write_text(
        json.dumps(
            {
                "agent_id": "examples.basic",
                "dataset": "default",
                "tags": ["tool_selection"],
                "split_strategy": "deterministic_multilabel_v1",
                "baseline_harness_version": baseline.to_payload(),
                "candidate_harness_version": candidate.to_payload(),
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    common = ["--storage-dsn", dsn]

    created = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "create",
            "--request-file",
            str(request_file),
            "--idempotency-key",
            "cli-key",
            "--request-id",
            "cli-create",
            *common,
        ],
    )
    replay = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "create",
            "--request-file",
            str(request_file),
            "--idempotency-key",
            "cli-key",
            "--request-id",
            "cli-replay",
            *common,
        ],
    )
    experiment_id = created["experiment_id"]
    shown = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "show",
            experiment_id,
            "--request-id",
            "cli-show",
            *common,
        ],
    )
    compared = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "compare",
            experiment_id,
            "--request-id",
            "cli-compare",
            *common,
        ],
    )
    accepted = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "accept",
            experiment_id,
            "--decision",
            "accepted",
            "--reason",
            "manual CLI review passed",
            "--accepted-harness-version",
            candidate.version_id,
            "--reviewer",
            "cli-reviewer",
            "--request-id",
            "cli-accept",
            *common,
        ],
    )
    secret = runner.invoke(
        app,
        [
            "eval",
            "experiment",
            "accept",
            experiment_id,
            "--decision",
            "rejected",
            "--reason",
            "api_key=cli-secret-123456789",
            "--request-id",
            "cli-secret",
            *common,
        ],
    )
    missing = runner.invoke(
        app,
        [
            "eval",
            "experiment",
            "show",
            "missing-experiment",
            "--request-id",
            "cli-missing",
            *common,
        ],
    )
    private_request_file = tmp_path / "private-experiment-input.json"
    private_request_file.write_text(
        json.dumps(
            {
                **json.loads(request_file.read_text(encoding="utf-8")),
                "evaluator_profile": {"name": "caller-controlled"},
                "metric_versions": {"unsafe_metric": "999"},
            }
        ),
        encoding="utf-8",
    )
    private_input = runner.invoke(
        app,
        [
            "eval",
            "experiment",
            "create",
            "--request-file",
            str(private_request_file),
            "--idempotency-key",
            "private-input-key",
            "--request-id",
            "cli-private-input",
            *common,
        ],
    )

    assert replay["experiment_id"] == experiment_id
    assert replay["request_id"] == "cli-replay"
    assert shown["experiment_id"] == experiment_id
    assert "comparison" not in created
    assert "comparison" not in shown
    assert compared["acceptance_recommendation"] == "accept"
    assert compared["request_id"] == "cli-compare"
    assert accepted["reviewer_id"] == "cli-reviewer"
    assert accepted["production_binding"] is True
    assert secret.exit_code == 1
    secret_error = json.loads(secret.stderr)
    assert secret_error["error"]["code"] == "validation_error"
    assert "cli-secret-123456789" not in secret.output
    assert missing.exit_code == 1
    assert json.loads(missing.stderr)["error"]["code"] == "eval.experiment.not_found"
    assert private_input.exit_code == 1
    assert json.loads(private_input.stderr)["error"]["code"] == "validation_error"
    assert table_count(db_path, "eval_experiments") == 1
    assert table_count(db_path, "harness_acceptance_records") == 1
    # PolicyEngine 的 decision audit 与原子 acceptance audit 各一条。
    assert table_count(db_path, "audit_logs") == 2


def test_phase_11_no_approved_cases_semantics_remain_stable(tmp_path: Path) -> None:
    """Experiment CLI 不能改变基础 approved-only runner 的空态。"""

    from agent_harness.evals import EvalRunner, ScoreSink

    result = asyncio.run(
        EvalRunner(score_sink=ScoreSink(local_path=tmp_path / "scores.jsonl")).run_file_dataset(
            dataset_dir=tmp_path / "empty-eval-cases",
            tenant_id="default",
            agent_id="examples.basic",
        )
    )
    assert result.status == "no_approved_cases"
    assert result.case_count == 0
    assert result.score_summary == {"case_count": 0}
    assert result.skipped_drafts == 0


def test_cli_reads_compressed_failure_difference_refs(tmp_path: Path) -> None:
    from agent_harness.evals import ExperimentCreateRequest, ExperimentService
    from agent_harness.storage import SQLAlchemyStorage, run_migrations

    db_path = tmp_path / "eval-experiment-cli-bounded-refs.db"
    dsn = sqlite_dsn(db_path)
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = _manifest("bounded-baseline")
    candidate = _manifest("bounded-candidate")
    _seed_approved_cases(
        storage=storage,
        baseline_id=baseline.version_id,
        candidate_id=candidate.version_id,
    )
    body = {
        "agent_id": "examples.basic",
        "dataset": "default",
        "tags": ["tool_selection"],
        "split_strategy": "deterministic_multilabel_v1",
        "baseline_harness_version": baseline.to_payload(),
        "candidate_harness_version": candidate.to_payload(),
    }

    async def create_boundary_experiment() -> str:
        request = ExperimentCreateRequest.model_validate(
            {
                **body,
                "tenant_id": "default",
                "request_id": "cli-bounded-create",
                "idempotency_key": "cli-bounded-key",
            }
        )
        outcome = await ExperimentService(
            storage=storage,
            evaluator=CliBoundaryCaseRefsEvaluator(
                baseline.version_id,
                candidate.version_id,
            ),
        ).create(request)
        await storage.dispose()
        return outcome.result.experiment_id

    experiment_id = asyncio.run(create_boundary_experiment())
    request_file = tmp_path / "bounded-experiment.json"
    request_file.write_text(json.dumps(body), encoding="utf-8")
    runner = CliRunner()
    common = ["--storage-dsn", dsn]
    replay = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "create",
            "--request-file",
            str(request_file),
            "--idempotency-key",
            "cli-bounded-key",
            "--request-id",
            "cli-bounded-replay",
            *common,
        ],
    )
    shown = _invoke(
        runner,
        ["eval", "experiment", "show", experiment_id, "--request-id", "cli-bounded-show", *common],
    )
    compared = _invoke(
        runner,
        [
            "eval",
            "experiment",
            "compare",
            experiment_id,
            "--request-id",
            "cli-bounded-compare",
            *common,
        ],
    )

    truth_ref = f"db://eval-experiments/{experiment_id}"
    assert replay["status"] == "completed"
    assert shown["status"] == "completed"
    assert compared["new_failures"][0]["evidence_refs"] == [truth_ref]
