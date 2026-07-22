"""集中定义 需求验收矩阵的稳定标识、producer 和测试映射策略。"""

from __future__ import annotations

import re

from ci_evidence import GATE_TARGETS

REQ_HEADING = re.compile(r"(?m)^### (REQ-\d+):")
AC_ID = re.compile(r"(?m)^- \[[ xX]\] (AC-\d+[A-Z]*):")
ALLOWED_STATUS = {"pass", "partial", "pending", "blocked", "hosted-unverified"}
REQUIRED_HEADERS = ["ID", "状态", "生产路径", "CI job", "测试", "Evidence"]
PLACEHOLDERS = {"", "-", "n/a", "none", "todo", "待定", "缺失"}
EVIDENCE_GATE_PATH = re.compile(r"^\.artifacts/ci/([a-z0-9][a-z0-9-]*)/result\.json$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
CI_RESULT_SCHEMA = "ci-result/v1"
CI_RESULT_STATUSES = {"pass", "fail", "skipped"}
KNOWN_EVIDENCE_GATES = set(GATE_TARGETS)
MULTI_VALUE_SEPARATOR = re.compile(r"\s*<br\s*/?>\s*", re.IGNORECASE)

# 自然语言 AC 中的复合行为不能退化为任意一个绿色 gate。这里只记录跨 gate
# 的特殊约束；普通能力仍由矩阵中的具体 pytest 测试和 producer 命令共同追踪。
REQUIRED_PRODUCER_GATES = {
    "AC-001": frozenset({"install"}),
    "AC-002": frozenset({"build"}),
    "AC-003": frozenset({"integration"}),
    "AC-006": frozenset({"integration"}),
    "AC-007": frozenset({"test-aggregate", "smoke-service"}),
    "AC-011": frozenset({"test-aggregate", "smoke-service"}),
    "AC-012": frozenset({"test-aggregate", "smoke-service"}),
    "AC-029": frozenset({"test-aggregate", "eval"}),
    "AC-050": frozenset({"acceptance-validate"}),
    "AC-051": frozenset(
        {
            "lock",
            "ruff-format",
            "ruff-lint",
            "pyright",
            "import-boundary",
            "quality-aggregate",
            "unit-contract",
            "integration",
            "test-aggregate",
        }
    ),
    "AC-052": frozenset({"eval"}),
    "AC-053": frozenset({"quality-aggregate", "eval", "smoke-local", "smoke-service"}),
    "AC-054": frozenset({"quality-aggregate", "eval", "smoke-local", "smoke-service"}),
    "AC-060": frozenset({"test-aggregate", "smoke-service"}),
    "AC-065": frozenset({"smoke-local"}),
    "AC-068": frozenset({"test-aggregate", "smoke-service"}),
}
REQUIRED_TEST_MAPPINGS = {
    "AC-003": frozenset(
        {
            "tests/integration/test_template_local_dev_example_smoke.py::"
            "test_copied_template_runs_local_dev_and_generated_example"
        }
    ),
    "AC-004": frozenset(
        {
            "tests/contracts/test_vendor_boundary_doctor_contracts.py::"
            "test_example_agents_have_no_direct_vendor_sdk_imports"
        }
    ),
    "AC-005": frozenset(
        {
            "tests/contracts/test_agent_registry_router_model_contracts.py::"
            "test_model_router_uses_fake_provider_and_reports_budget_fallback"
        }
    ),
    "AC-006": frozenset(
        {
            "tests/integration/test_template_local_dev_example_smoke.py::"
            "test_copied_template_runs_local_dev_and_generated_example"
        }
    ),
    "AC-007": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup"
        }
    ),
    "AC-011": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios"
        }
    ),
    "AC-012": frozenset(
        {
            "tests/contracts/test_storage_migration_uow_contracts.py::"
            "test_repository_contract_uses_uow_and_rolls_back",
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios",
        }
    ),
    "AC-019": frozenset(
        {
            "tests/contracts/test_runtime_checkpoint_orchestrator_contracts.py::"
            "test_default_identity_propagates_to_run_session_trace_and_eval"
        }
    ),
    "AC-023": frozenset(
        {
            "tests/contracts/test_dev_approval_flows_contracts.py::"
            "test_dev_deny_and_known_tool_failure_keep_approval_semantics"
        }
    ),
    "AC-026": frozenset(
        {
            "tests/contracts/test_tool_registry_authorization_contracts.py::"
            "test_tool_registry_preflight_errors_are_not_masked_by_approval"
        }
    ),
    "AC-029": frozenset(
        {
            "tests/contracts/test_example_eval_migration_contracts.py::"
            "test_example_eval_uses_fake_model_without_real_provider_keys"
        }
    ),
    "AC-052": frozenset(
        {
            "tests/contracts/test_example_eval_migration_contracts.py::"
            "test_example_eval_uses_fake_model_without_real_provider_keys"
        }
    ),
    "AC-060": frozenset(
        {
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_uses_http_auth_crash_reclaim_checkpoint_and_scoped_cleanup"
        }
    ),
    "AC-061": frozenset(
        {
            "tests/contracts/test_vendor_boundary_doctor_contracts.py::"
            "test_business_agents_have_no_vendor_or_orm_session_imports"
        }
    ),
    "AC-062": frozenset(
        {
            "tests/contracts/test_runtime_checkpoint_template_contracts.py::"
            "test_template_api_helper_uses_runtime_seam",
            "tests/contracts/test_service_worker_shared_identity_contracts.py::"
            "test_service_submit_and_worker_execute_share_run_and_identity",
            "tests/contracts/test_tool_registry_public_seam_contracts.py::"
            "test_tool_registry_public_seam_enforces_errors_policy_and_output_metadata",
            "tests/contracts/test_model_usage_runtime_composition_contracts.py::"
            "test_rag_runtime_composition_emits_correlated_model_and_embedding_usage",
            "tests/contracts/test_sse_http_openapi_contracts.py::"
            "test_run_003_and_run_006_expose_the_same_public_envelopes",
        }
    ),
    "AC-065": frozenset(
        {
            "tests/contracts/test_model_usage_smoke_contracts.py::"
            "test_public_local_fake_run_completes_under_fixed_threshold"
        }
    ),
    "AC-068": frozenset(
        {
            "tests/contracts/test_shared_parent_budget_repository_competition_contracts.py::"
            "test_sqlite_true_concurrency_commits_only_safe_direct_combination",
            "tests/contracts/test_service_deployment_packaging_smoke_contracts.py::"
            "test_service_smoke_executes_postgresql_migration_and_shared_budget_scenarios",
        }
    ),
}
