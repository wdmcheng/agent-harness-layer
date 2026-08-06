"""需求验收矩阵中行为测试与真实 producer 的语义映射合同。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.contracts.acceptance_matrix_test_support import (
    ROOT,
    run_matrix_validator,
    write_gate_result,
)


def _acceptance_policy_mapping(name: str) -> dict[str, set[str]]:
    """从策略源码读取稳定常量，不执行其脚本级依赖。"""

    tree = ast.parse((ROOT / "scripts/acceptance_matrix_policy.py").read_text(encoding="utf-8"))
    assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    )
    assert isinstance(assignment.value, ast.Dict)
    return {
        ast.literal_eval(key): set(ast.literal_eval(value.args[0]))
        for key, value in zip(assignment.value.keys, assignment.value.values, strict=True)
        if key is not None and isinstance(value, ast.Call) and value.args
    }


@pytest.mark.parametrize(
    ("identifier", "required_gate"),
    [
        ("AC-001", "install"),
        ("AC-002", "build"),
        ("AC-003", "integration"),
        ("AC-011", "smoke-service"),
        ("AC-012", "smoke-service"),
        ("AC-068", "smoke-service"),
    ],
)
def test_validator_rejects_non_executing_producer_for_semantic_acceptance(
    tmp_path: Path,
    identifier: str,
    required_gate: str,
) -> None:
    """命令、PostgreSQL 与并发 AC 必须由真正执行对应行为的 producer 证明。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/packaging.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_packaging.py").write_text(
        "def test_packaging():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
    )
    test_references = ["tests/contracts/test_packaging.py::test_packaging"]
    if identifier == "AC-003":
        test_path = Path("tests/integration/test_template_local_dev_example_smoke.py")
        (tmp_path / "tests/integration").mkdir(parents=True)
        (tmp_path / test_path).write_text(
            "def test_copied_template_runs_local_dev_and_generated_example():\n"
            "    value = 1\n"
            "    assert value == 1\n",
            encoding="utf-8",
        )
        test_references = [
            f"{test_path.as_posix()}::test_copied_template_runs_local_dev_and_generated_example"
        ]
    elif identifier in {"AC-011", "AC-012", "AC-068"}:
        service_path = Path("tests/contracts/test_service_deployment_packaging_smoke_contracts.py")
        (tmp_path / service_path).write_text(
            "def test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios():\n"
            "    value = 1\n"
            "    assert value == 1\n",
            encoding="utf-8",
        )
        test_references = [
            f"{service_path.as_posix()}::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios"
        ]
        if identifier == "AC-012":
            storage_path = Path("tests/contracts/test_storage_migration_uow_contracts.py")
            (tmp_path / storage_path).write_text(
                "def test_repository_contract_uses_uow_and_rolls_back():\n"
                "    value = 1\n"
                "    assert value == 1\n",
                encoding="utf-8",
            )
            test_references.insert(
                0,
                f"{storage_path.as_posix()}::test_repository_contract_uses_uow_and_rolls_back",
            )
        elif identifier == "AC-068":
            sqlite_path = Path(
                "tests/contracts/test_shared_parent_budget_repository_competition_contracts.py"
            )
            (tmp_path / sqlite_path).write_text(
                "def test_sqlite_true_concurrency_commits_only_safe_direct_combination():\n"
                "    value = 1\n"
                "    assert value == 1\n",
                encoding="utf-8",
            )
            test_references.insert(
                0,
                f"{sqlite_path.as_posix()}::"
                "test_sqlite_true_concurrency_commits_only_safe_direct_combination",
            )
    test_cell = "<br>".join(f"`{reference}`" for reference in test_references)
    spec.write_text(
        f"### REQ-001: 包安装与构建\n\n- [ ] {identifier}: 执行真实命令\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-001 | partial | `src/packaging.py` | `ci-contract` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/ci-contract/result.json` |\n"
        f"| {identifier} | pass | `src/packaging.py` | `test-aggregate` | "
        f"{test_cell} | `.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "ci-contract")
    write_gate_result(tmp_path, "test-aggregate")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "required CI producers" in rejected.stderr
    assert required_gate in rejected.stderr


def test_packaging_acceptance_rows_map_to_command_producers() -> None:
    """仓库矩阵不得用 pytest 聚合冒充真实安装或构建命令。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac001 = next(line for line in rows if line.startswith("| AC-001 |"))
    ac002 = next(line for line in rows if line.startswith("| AC-002 |"))

    assert "| `install` |" in ac001
    assert ".artifacts/ci/install/result.json" in ac001
    assert "| `build` |" in ac002
    assert ".artifacts/ci/build/result.json" in ac002


def test_ac003_maps_to_external_wheel_install_integration() -> None:
    """AC-003 必须指向 workspace 外安装 wheel 并启动模板的真实集成测试。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac003 = next(line for line in rows if line.startswith("| AC-003 |"))

    assert "tests/integration/test_template_local_dev_example_smoke.py" in ac003
    assert "`integration`" in ac003
    assert ".artifacts/ci/integration/result.json" in ac003


def test_validator_rejects_non_external_wheel_test_for_ac003(tmp_path: Path) -> None:
    """AC-003 的测试路径必须实际执行 workspace 外 wheel 安装，不能只读 Dockerfile。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "tests/integration").mkdir(parents=True)
    (tmp_path / "src/packaging.py").write_text("VALUE = 1\n", encoding="utf-8")
    for relative in (
        "tests/contracts/test_packaging.py",
        "tests/integration/test_template_local_dev_example_smoke.py",
    ):
        (tmp_path / relative).write_text(
            "def test_packaging():\n    value = 1\n    assert value == 1\n", encoding="utf-8"
        )
    spec.write_text(
        "### REQ-001: 包安装与构建\n\n- [ ] AC-003: workspace 外安装 wheel 并运行模板\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-001 | partial | `src/packaging.py` | `ci-contract` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/ci-contract/result.json` |\n"
        "| AC-003 | pass | `src/packaging.py` | `integration` | "
        "`tests/contracts/test_packaging.py::test_packaging` | "
        "`.artifacts/ci/integration/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "ci-contract")
    write_gate_result(tmp_path, "integration")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "required test mappings" in rejected.stderr


@pytest.mark.parametrize("identifier", ["AC-011", "AC-012", "AC-068"])
def test_postgresql_acceptance_rows_map_to_real_service_smoke(identifier: str) -> None:
    """需要真实 PostgreSQL 的验收不能映射到 clean test job 中会跳过的用例。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    row = next(line for line in rows if line.startswith(f"| {identifier} |"))

    assert "`smoke-service`" in row
    assert ".artifacts/ci/smoke-service/result.json" in row
    assert "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios" in row
    if identifier in {"AC-012", "AC-068"}:
        assert "`test-aggregate`" in row
        assert ".artifacts/ci/test-aggregate/result.json" in row


def test_ac012_and_ac068_map_sqlite_and_postgresql_behavior_separately() -> None:
    """复合后端验收必须同时保留 SQLite 精确节点与真实 PostgreSQL producer。"""

    rows = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
    ac012 = next(line for line in rows if line.startswith("| AC-012 |"))
    ac068 = next(line for line in rows if line.startswith("| AC-068 |"))

    assert "test_repository_contract_uses_uow_and_rolls_back" in ac012
    assert "test_sqlite_true_concurrency_commits_only_safe_direct_combination" in ac068
    for row in (ac012, ac068):
        assert "`test-aggregate`<br>`smoke-service`" in row
        assert ".artifacts/ci/test-aggregate/result.json" in row
        assert ".artifacts/ci/smoke-service/result.json" in row


def test_model_tool_intent_and_loop_acceptance_policy_freezes_exact_required_evidence() -> None:
    """工具意图/循环验收必须锁定实际producer与公共节点，不能只靠矩阵文本。"""

    expected_tests = {
        "AC-104": {
            "tests/contracts/test_provider_neutral_tool_catalog_contracts.py::test_provider_catalog_matches_frozen_golden_vector_byte_for_byte",
            "tests/contracts/test_tool_intent_usage_settlement_contracts.py::test_tool_intent_success_and_exact_replay_settle_usage_once",
            "tests/contracts/test_tool_intent_model_catalog_config_contracts.py::test_tool_intent_route_binds_actual_catalog_bytes_and_dynamic_budget",
        },
        "AC-105": {
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_resolve_intent_fails_closed_without_execution_side_effects",
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_call_revalidates_resolved_intent_before_policy_or_handler",
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_call_approved_revalidates_before_claim_or_handler",
            "tests/contracts/test_tool_intent_policy_approval_contracts.py::test_synchronized_tool_recovery_tamper_fails_before_provider",
            "tests/contracts/test_policy_gated_model_tool_loop_policy_contracts.py::test_initial_loop_registry_drift_writes_redacted_validation_before_side_effects",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_resume_recovery_contracts.py::test_approval_resume_registry_drift_writes_validation_before_tool_effects",
        },
        "AC-106": {
            "tests/contracts/test_policy_gated_model_tool_loop_policy_contracts.py::test_real_policy_three_states_preserve_evidence_and_zero_execution",
            "tests/contracts/test_policy_gated_model_tool_loop_approval_public_seam_contracts.py::test_waiting_snapshot_resumes_through_call_approved_exactly_once",
            "tests/contracts/test_policy_gated_model_tool_loop_sqlite_resume_contracts.py::test_sqlite_reload_resumes_exact_snapshot_and_executes_tool_at_most_once",
        },
        "AC-107": {
            "tests/contracts/test_policy_gated_model_tool_loop_result_guard_contracts.py::test_sensitive_success_is_redacted_before_context_reinjection",
            "tests/contracts/test_policy_gated_model_tool_loop_result_guard_contracts.py::test_truncated_success_reinjects_only_the_artifact_reference",
            "tests/contracts/test_policy_gated_model_tool_loop_event_contracts.py::test_full_loop_emits_linear_correlated_content_free_events",
        },
        "AC-108": {
            "tests/contracts/test_policy_gated_tool_loop_registry_config_contracts.py::test_invalid_loop_shape_or_root_budget_fails_before_executor_import",
            "tests/contracts/test_policy_gated_tool_loop_registry_config_contracts.py::test_model_tool_loop_is_required_iff_route_supports_tool_intent",
            "tests/contracts/test_policy_gated_model_tool_loop_limits_contracts.py::test_limit_overrides_accept_null_inheritance_and_individual_shrinking",
            "tests/contracts/test_policy_gated_model_tool_loop_approval_public_seam_contracts.py::test_approval_resume_reuses_original_deadline_and_balance_before_handler",
        },
        "AC-109": {
            "tests/contracts/test_model_tool_exact_result_context_replay_contracts.py::test_completed_result_replays_same_untrusted_context_without_handler",
            "tests/contracts/test_model_tool_handler_not_started_takeover_contracts.py::test_only_one_worker_can_take_over_expired_claim_and_old_fence_stops",
            "tests/contracts/test_model_tool_turn_usage_replay_contracts.py::test_loop_turn_usage_exact_replay_is_single_settlement_with_bound_identity",
            "tests/contracts/test_model_tool_approval_recovery_freshness_contracts.py::test_active_fresh_grant_resolves_but_stale_lease_is_expired",
            "tests/contracts/test_policy_gated_model_tool_loop_event_recovery_contracts.py::test_public_run_recovery_republishes_exact_event_and_completes_unique_loop",
            "tests/contracts/test_policy_gated_model_tool_loop_event_fencing_contracts.py::test_tool_claim_and_event_reservation_share_first_owner_commit",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py::test_approved_model_claim_and_events_share_first_owner_commit",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py::test_sqlite_concurrent_approved_calls_prepare_only_the_actual_owner",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py::test_postgresql_concurrent_approved_calls_prepare_only_the_actual_owner",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py::test_sqlite_concurrent_unapproved_calls_wait_and_replay_exact_result",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_event_atomicity_contracts.py::test_postgresql_concurrent_unapproved_calls_wait_and_replay_exact_result",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_resume_recovery_contracts.py::test_sqlite_approved_exact_replay_recovers_pending_final_event",
            "tests/contracts/test_model_tool_loop_postgresql_contracts.py::test_postgresql_approved_exact_replay_recovers_pending_final_event",
        },
        "AC-110": {
            "tests/contracts/test_model_tool_execution_unknown_needs_review_contracts.py::test_executing_recovery_persists_claim_and_loop_needs_review",
            "tests/contracts/test_model_tool_execution_unknown_needs_review_contracts.py::test_unknown_event_version_uses_same_needs_review_branch",
            "tests/contracts/test_model_tool_loop_terminal_fencing_contracts.py::test_active_terminal_and_turn_competitors_have_one_winner",
            "tests/contracts/test_model_tool_loop_shared_budget_recovery_contracts.py::test_unknown_tool_effect_fences_same_parent_ledger_without_releasing_model_impact",
            "tests/contracts/test_policy_gated_model_tool_loop_event_recovery_contracts.py::test_public_run_recovery_fences_unknown_event_version_without_reexecution",
            "tests/contracts/test_policy_gated_model_tool_loop_event_contracts.py::test_unapproved_handler_unknown_fences_public_loop",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_resume_recovery_contracts.py::test_approved_handler_unknown_fences_public_loop",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_sqlite_handler_unknown_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_postgresql_handler_unknown_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_sqlite_result_guard_failure_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_postgresql_result_guard_failure_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_sqlite_result_artifact_failure_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_postgresql_result_artifact_failure_fences_normal_and_approved_public_entries",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_sqlite_result_commit_ack_unknown_replays_exact_terminal",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_postgresql_result_commit_ack_unknown_replays_exact_terminal",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_sqlite_result_commit_unknown_without_terminal_fences",
            "tests/contracts/test_policy_gated_model_tool_loop_result_lifecycle_contracts.py::test_postgresql_result_commit_unknown_without_terminal_fences",
        },
        "AC-111": {
            "tests/contracts/test_model_tool_loop_migration_contracts.py::test_0018_sqlite_upgrade_preserves_legacy_tool_and_context_rows",
            "tests/contracts/test_model_tool_loop_marker_downgrade_contracts.py::test_tool_or_context_v1_evidence_commits_in_the_same_uow",
            "tests/contracts/test_model_tool_loop_postgresql_contracts.py::test_postgresql_repository_concurrency_cancel_and_budget_fencing",
            "tests/contracts/test_documentation_bilingual_contracts.py::test_deep_documentation_link_chain_is_bilingual",
            "tests/contracts/test_controlled_real_model_offline_contracts.py::test_default_gates_ignore_provider_credentials_and_network",
        },
    }
    expected_gates = {
        "AC-104": {"test-aggregate"},
        "AC-105": {"test-aggregate"},
        "AC-106": {"test-aggregate"},
        "AC-107": {"test-aggregate"},
        "AC-108": {"test-aggregate"},
        "AC-109": {"test-aggregate"},
        "AC-110": {"test-aggregate"},
        "AC-111": {"test-aggregate", "smoke-service"},
    }

    required_tests = _acceptance_policy_mapping("REQUIRED_TEST_MAPPINGS")
    required_gates = _acceptance_policy_mapping("REQUIRED_PRODUCER_GATES")
    assert {
        identifier: required_tests[identifier] for identifier in expected_tests
    } == expected_tests
    assert {
        identifier: required_gates[identifier] for identifier in expected_gates
    } == expected_gates


def test_ac006_maps_to_real_copied_template_dev_and_example_smoke() -> None:
    """AC-006 不能继续映射只 mock server 或静态查看 Makefile 的测试。"""

    matrix = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-006 |"))

    assert "tests/integration/test_template_local_dev_example_smoke.py" in row
    assert "tests/contracts/test_service_app_template_maintenance_contracts.py" not in row


def test_ac065_maps_to_full_local_smoke_latency_producer() -> None:
    """AC-065 的整轮耗时必须由 local smoke producer 证明，不能映射 SSE 首帧测试。"""

    matrix = (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8")
    row = next(line for line in matrix.splitlines() if line.startswith("| AC-065 |"))

    assert "scripts/smoke_local.py" in row
    assert (
        "tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold" in row
    )
    assert "`smoke-local`" in row
    assert ".artifacts/ci/smoke-local/result.json" in row
    assert "test_sse_first_frame_performance.py" not in row


def test_reviewed_semantic_acceptance_rows_use_behavioral_nodes() -> None:
    """审查点名的 AC 必须映射到实际执行该行为的测试，不能只满足 AST 形状。"""

    rows = {
        line.split("|", 2)[1].strip(): line
        for line in (ROOT / "docs/acceptance-matrix.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| AC-")
    }
    required_nodes = {
        "AC-004": ["test_example_agents_have_no_direct_vendor_sdk_imports"],
        "AC-005": ["test_model_router_uses_fake_provider_and_reports_budget_fallback"],
        "AC-019": ["test_default_identity_propagates_to_run_session_trace_and_eval"],
        "AC-023": ["test_dev_deny_and_known_tool_failure_keep_approval_semantics"],
        "AC-026": ["test_tool_registry_preflight_errors_are_not_masked_by_approval"],
        "AC-029": ["test_example_eval_uses_fake_model_without_real_provider_keys"],
        "AC-052": ["test_example_eval_uses_fake_model_without_real_provider_keys"],
        "AC-061": ["test_business_agents_have_no_vendor_or_orm_session_imports"],
        "AC-062": [
            "test_template_api_helper_uses_runtime_seam",
            "test_service_submit_and_worker_execute_share_run_and_identity",
            "test_tool_registry_public_seam_enforces_errors_policy_and_output_metadata",
            "test_rag_runtime_composition_emits_correlated_model_and_embedding_usage",
            "test_run_003_and_run_006_expose_the_same_public_envelopes",
        ],
    }
    rejected_nodes = {
        "test_boundary_contract_lists_banned_vendors_and_adapter_allowlist",
        "test_orchestrator_uses_typed_executor_and_has_no_fake_fallback",
        "test_env_example_local_profile_uses_default_identity_without_authorization",
        "test_policy_engine_guardrail_approval_and_audit_flow",
        "test_tool_registry_enforces_agent_tool_allowlist",
        "test_file_eval_optional_executor_preserves_drafts_and_scores_real_output",
        "test_identity_and_permission_context_keep_tenant_session_fields",
    }

    for identifier, nodes in required_nodes.items():
        row = rows[identifier]
        assert all(node in row for node in nodes), identifier
        assert not any(node in row for node in rejected_nodes), identifier


def test_validator_rejects_non_smoke_producer_for_ac065(tmp_path: Path) -> None:
    """即使文件与 evidence 都存在，AC-065 缺少 smoke-local producer 也必须失败。"""

    spec = tmp_path / "Product-Spec.md"
    matrix = tmp_path / "matrix.md"
    (tmp_path / "src").mkdir()
    (tmp_path / "tests/contracts").mkdir(parents=True)
    (tmp_path / "src/smoke.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "tests/contracts/test_model_usage_smoke_contracts.py").write_text(
        "def test_public_local_fake_run_completes_under_fixed_threshold():\n"
        "    value = 1\n"
        "    assert value == 1\n",
        encoding="utf-8",
    )
    spec.write_text(
        "### REQ-022: 性能\n\n- [ ] AC-065: local fake provider 单 agent 完整执行小于 5 秒\n",
        encoding="utf-8",
    )
    matrix.write_text(
        "# 需求验收矩阵\n\n"
        "| ID | 状态 | 生产路径 | CI job | 测试 | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| REQ-022 | partial | `src/smoke.py` | `test-aggregate` | "
        "`tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n"
        "| AC-065 | partial | `src/smoke.py` | `test-aggregate` | "
        "`tests/contracts/test_model_usage_smoke_contracts.py::"
        "test_public_local_fake_run_completes_under_fixed_threshold` | "
        "`.artifacts/ci/test-aggregate/result.json` |\n",
        encoding="utf-8",
    )
    write_gate_result(tmp_path, "test-aggregate")

    rejected = run_matrix_validator(spec, matrix)

    assert rejected.returncode == 2
    assert "smoke-local" in rejected.stderr
