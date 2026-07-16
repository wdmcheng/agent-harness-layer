"""评测实验比较持久化与 fail-closed 合同测试。"""

from __future__ import annotations

from tests.contracts.test_eval_experiment_comparison_contracts import (
    FailingPublisher as FailingPublisher,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    Path as Path,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    RecordingEvaluator as RecordingEvaluator,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    _evaluation as _evaluation,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    _harness_sources as _harness_sources,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    json as json,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    pytest as pytest,
)
from tests.contracts.test_eval_experiment_comparison_contracts import (
    sqlite_dsn as sqlite_dsn,
)


@pytest.mark.asyncio
async def test_experiment_service_reuses_split_persists_local_first_and_degrades_provider(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import ExperimentRequest, ExperimentService, HarnessVersionBuilder
    from agent_harness.storage import EvalDatasetSplitCreate, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "experiment-service.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = HarnessVersionBuilder().build(_harness_sources())
    candidate_sources = _harness_sources()
    candidate_sources["prompt_instruction"].value = {"system": "answer with cited evidence"}
    candidate = HarnessVersionBuilder().build(candidate_sources)
    scores = {
        baseline.version_id: _evaluation(
            baseline.version_id,
            {
                "opt": ("optimization", ["tool_selection"], 0.5, True),
                "hold": ("holdout", ["tool_selection"], 0.8, True),
            },
        ),
        candidate.version_id: _evaluation(
            candidate.version_id,
            {
                "opt": ("optimization", ["tool_selection"], 0.7, True),
                "hold": ("holdout", ["tool_selection"], 0.8, True),
            },
        ),
    }
    evaluator = RecordingEvaluator(scores)
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id="split-1",
                    tenant_id="tenant-a",
                    agent_id="examples.basic",
                    dataset="default",
                    request_id="split-request",
                    tags=["tool_selection"],
                    strategy="deterministic_multilabel_v1",
                    optimization_ratio=0.8,
                    holdout_ratio=0.2,
                    case_tags={
                        "opt": ["tool_selection"],
                        "hold": ["tool_selection"],
                    },
                    optimization_case_ids=["opt"],
                    holdout_case_ids=["hold"],
                    regression_case_ids=[],
                )
            )
            await uow.commit()

        service = ExperimentService(
            storage=storage,
            evaluator=evaluator,
            publishers=[FailingPublisher()],
        )
        result = await service.run(
            ExperimentRequest(
                request_id="request-1",
                tenant_id="tenant-a",
                idempotency_key="experiment-key",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-1",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )

        assert result.status == "completed_with_degradation"
        assert result.comparison is not None
        assert result.comparison.acceptance_recommendation == "accept"
        assert result.local_evidence_refs
        assert result.provider_statuses[0]["status"] == "degraded"
        assert "provider-secret" not in json.dumps(result.to_payload())
        assert len(evaluator.calls) == 2
        assert evaluator.calls[0]["split"].split_id == evaluator.calls[1]["split"].split_id
        assert evaluator.calls[0]["evaluator_profile"] == evaluator.calls[1]["evaluator_profile"]

        replay = await service.run(
            ExperimentRequest(
                request_id="request-retry",
                tenant_id="tenant-a",
                idempotency_key="experiment-key",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-1",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert replay.experiment_id == result.experiment_id
        assert len(evaluator.calls) == 2
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_baseline_only_cross_tenant_and_partial_failure_are_fail_closed(
    tmp_path: Path,
) -> None:
    from agent_harness.evals import (
        EvalExperimentError,
        ExperimentRequest,
        ExperimentService,
        HarnessVersionBuilder,
    )
    from agent_harness.storage import EvalDatasetSplitCreate, SQLAlchemyStorage, run_migrations

    dsn = sqlite_dsn(tmp_path / "experiment-boundaries.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    baseline = HarnessVersionBuilder().build(_harness_sources())
    candidate_sources = _harness_sources()
    candidate_sources["prompt_instruction"].value = {"system": "candidate prompt"}
    candidate = HarnessVersionBuilder().build(candidate_sources)
    baseline_result = _evaluation(
        baseline.version_id,
        {
            "opt": ("optimization", ["tool_selection"], 0.5, True),
            "hold": ("holdout", ["tool_selection"], 0.8, True),
        },
    )
    partial_candidate = _evaluation(
        candidate.version_id,
        {"opt": ("optimization", ["tool_selection"], 0.7, True)},
    )
    evaluator = RecordingEvaluator(
        {baseline.version_id: baseline_result},
        fail_candidate=True,
        partial_candidate=partial_candidate,
    )
    try:
        async with storage.uow() as uow:
            await uow.tenants.ensure("tenant-a")
            await uow.tenants.ensure("tenant-b")
            await uow.eval_dataset_splits.create(
                EvalDatasetSplitCreate(
                    split_id="split-boundaries",
                    tenant_id="tenant-a",
                    agent_id="examples.basic",
                    dataset="default",
                    request_id="split-request",
                    tags=["tool_selection"],
                    strategy="deterministic_multilabel_v1",
                    optimization_ratio=0.8,
                    holdout_ratio=0.2,
                    case_tags={
                        "opt": ["tool_selection"],
                        "hold": ["tool_selection"],
                    },
                    optimization_case_ids=["opt"],
                    holdout_case_ids=["hold"],
                    regression_case_ids=[],
                )
            )
            await uow.commit()

        service = ExperimentService(storage=storage, evaluator=evaluator)
        baseline_only = await service.run(
            ExperimentRequest(
                request_id="baseline-request",
                tenant_id="tenant-a",
                idempotency_key="baseline-only",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-boundaries",
                baseline_harness_version=baseline,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert baseline_only.status == "baseline_completed"
        with pytest.raises(EvalExperimentError) as missing:
            await service.compare(
                tenant_id="tenant-a",
                experiment_id=baseline_only.experiment_id,
                request_id="compare-request",
            )
        assert missing.value.code == "eval.experiment.candidate_missing"

        with pytest.raises(EvalExperimentError) as hidden:
            await service.get(
                tenant_id="tenant-b",
                experiment_id=baseline_only.experiment_id,
                request_id="hidden-request",
            )
        assert hidden.value.code == "eval.experiment.not_found"

        failed = await service.run(
            ExperimentRequest(
                request_id="failed-request",
                tenant_id="tenant-a",
                idempotency_key="candidate-fails",
                agent_id="examples.basic",
                dataset="default",
                split_id="split-boundaries",
                baseline_harness_version=baseline,
                candidate_harness_version=candidate,
                evaluator_profile={"name": "exact-match", "version": "1"},
                metric_versions={"exact_match": "1"},
            )
        )
        assert failed.status == "failed"
        assert failed.local_evidence_refs
        async with storage.uow() as uow:
            stored = await uow.eval_experiments.get("tenant-a", failed.experiment_id)
        assert stored is not None
        assert "evaluator-secret" not in json.dumps(stored.score_summaries)
        assert stored.score_summaries["baseline"]["case_results"]
        assert stored.score_summaries["candidate"]["case_results"][0]["case_id"] == "opt"
    finally:
        await storage.dispose()
