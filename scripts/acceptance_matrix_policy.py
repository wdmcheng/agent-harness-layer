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
    "AC-069": frozenset({"test-aggregate"}),
    "AC-070": frozenset({"lock", "install", "license", "release-dry-run", "test-aggregate"}),
    "AC-071": frozenset({"test-aggregate"}),
    "AC-072": frozenset({"test-aggregate", "release-dry-run"}),
    "AC-077": frozenset({"test-aggregate"}),
    "AC-078": frozenset({"test-aggregate"}),
    "AC-079": frozenset({"test-aggregate"}),
    "AC-080": frozenset({"test-aggregate"}),
    "AC-081": frozenset({"test-aggregate", "smoke-live-model"}),
    "AC-082": frozenset({"test-aggregate"}),
    "AC-083": frozenset(
        {"quality-aggregate", "test-aggregate", "eval", "smoke-local", "smoke-live-model"}
    ),
    "AC-084": frozenset({"test-aggregate"}),
    "AC-089": frozenset({"test-aggregate"}),
    "AC-096": frozenset({"test-aggregate"}),
    "AC-097": frozenset({"test-aggregate"}),
    "AC-098": frozenset({"test-aggregate"}),
    "AC-099": frozenset({"test-aggregate"}),
    "AC-100": frozenset({"test-aggregate"}),
    "AC-101": frozenset({"test-aggregate"}),
    "AC-102": frozenset({"test-aggregate", "eval"}),
    "AC-103": frozenset(
        {
            "quality-aggregate",
            "test-aggregate",
            "eval",
            "smoke-local",
            "smoke-service",
            "build",
            "license",
            "acceptance-validate",
        }
    ),
    "AC-104": frozenset({"test-aggregate"}),
    "AC-105": frozenset({"test-aggregate"}),
    "AC-106": frozenset({"test-aggregate"}),
    "AC-107": frozenset({"test-aggregate"}),
    "AC-108": frozenset({"test-aggregate"}),
    "AC-109": frozenset({"test-aggregate"}),
    "AC-110": frozenset({"test-aggregate"}),
    "AC-111": frozenset({"test-aggregate", "smoke-service"}),
}
REQUIRED_TEST_MAPPINGS = {
    "AC-077": frozenset(
        {
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_settings_merge_controlled_deployment_and_ignore_provider_ambient_env",
            "tests/contracts/test_controlled_real_model_runtime_composition_contracts.py::test_openai_sdk_ambient_env_cannot_change_controlled_client_or_outbound_request",
        }
    ),
    "AC-078": frozenset(
        {
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_real_deployment_loads_complete_typed_policy_without_secret_serialization",
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_model_catalog_rejects_unknown_mismatched_or_self_reported_bounds_before_reservation",
        }
    ),
    "AC-079": frozenset(
        {
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_endpoint_and_credential_origin_fail_closed_before_client_or_network",
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_endpoint_policy_catalog_rejects_unknown_or_mismatched_identity_before_runtime_side_effects",
        }
    ),
    "AC-080": frozenset(
        {
            "tests/contracts/test_controlled_real_model_routing_contracts.py::test_route_plan_intersects_deployment_agent_and_request_before_side_effects",
            "tests/contracts/test_controlled_real_model_routing_contracts.py::test_provider_identity_assertion_matches_deployment_kind_and_bound_adapter",
        }
    ),
    "AC-081": frozenset(
        {
            "tests/contracts/test_controlled_real_model_runtime_composition_contracts.py::test_composition_registers_async_real_provider_double_and_keeps_fake_offline",
            "tests/contracts/test_controlled_real_model_runtime_composition_contracts.py::test_openai_sdk_ambient_env_cannot_change_controlled_client_or_outbound_request",
            "tests/integration/test_controlled_real_model_live_smoke.py::test_opt_in_real_text_completion",
        }
    ),
    "AC-082": frozenset(
        {
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_invalid_dynamic_route_rejects_before_reservation_client_and_network",
            "tests/contracts/test_controlled_real_model_policy_approval_contracts.py::test_model_policy_coordinates_and_audit_precede_reservation",
            "tests/contracts/test_controlled_real_model_policy_approval_contracts.py::test_require_approval_creates_durable_checkpoint_with_zero_model_side_effects",
            "tests/contracts/test_controlled_real_model_policy_approval_contracts.py::test_bound_approval_grant_rechecks_hard_budget_and_invokes_provider_once",
            "tests/contracts/test_controlled_real_model_policy_approval_contracts.py::test_mismatched_stale_or_replayed_grant_fails_closed",
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_route_reservation_bound_and_adapter_output_cap_are_enforced_before_send",
            "tests/contracts/test_controlled_real_model_retry_budget_contracts.py::test_retry_requires_trusted_versioned_completion_signal",
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_execution_order_and_each_pre_send_failure_boundary_are_fenced",
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_pre_mark_cancel_rolls_back_reservation_permit_and_client_without_network",
            "tests/contracts/test_controlled_real_model_retry_settlement_contracts.py::test_retry_attempts_reserve_and_settle_only_trusted_actual_usage",
            "tests/contracts/test_controlled_real_model_replay_fencing_contracts.py::test_started_completed_or_failed_without_usage_keeps_reservation_and_fences_terminal",
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_bulkhead_deadline_cancel_and_unknown_are_fenced",
            "tests/contracts/test_controlled_real_model_budget_snapshot_contracts.py::test_budget_tree_v2_repository_validator_freezes_deployment_and_allowed_models",
        }
    ),
    "AC-083": frozenset(
        {
            "tests/contracts/test_controlled_real_model_offline_contracts.py::test_default_gates_ignore_provider_credentials_and_network",
            "tests/contracts/test_controlled_real_model_runtime_composition_contracts.py::test_openai_sdk_ambient_env_cannot_change_controlled_client_or_outbound_request",
            "tests/contracts/test_controlled_real_model_offline_contracts.py::test_live_smoke_reports_hosted_unverified_without_opt_in",
            "tests/contracts/test_controlled_real_model_offline_contracts.py::test_live_smoke_gate_is_allowlisted_and_maps_statuses_truthfully",
        }
    ),
    "AC-084": frozenset(
        {
            "tests/contracts/test_controlled_real_model_routing_contracts.py::test_missing_price_credential_or_capability_rejects_before_provider",
            "tests/contracts/test_controlled_real_model_config_contracts.py::test_endpoint_policy_catalog_rejects_unknown_or_mismatched_identity_before_runtime_side_effects",
            "tests/contracts/test_controlled_real_model_deadline_order_contracts.py::test_invalid_dynamic_route_rejects_before_reservation_client_and_network",
        }
    ),
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
    "AC-069": frozenset(
        {
            "tests/contracts/test_dependency_version_policy_contracts.py::"
            "test_all_python_dependency_declarations_use_reviewed_compatible_ranges"
        }
    ),
    "AC-070": frozenset(
        {
            "tests/contracts/test_dependency_version_policy_contracts.py::"
            "test_uv_range_and_conflicting_groups_keep_ci_environment_concrete",
            "tests/contracts/test_dependency_version_policy_contracts.py::"
            "test_lock_package_identities_match_reviewed_baseline",
        }
    ),
    "AC-071": frozenset(
        {
            "tests/contracts/test_dependency_version_policy_contracts.py::"
            "test_uv_range_and_conflicting_groups_keep_ci_environment_concrete",
            "tests/contracts/test_release_promotion_contracts.py::"
            "test_update_release_files_keeps_workspace_self_dependencies_exact",
        }
    ),
    "AC-072": frozenset(
        {
            "tests/contracts/test_release_four_stage_handoff_contracts.py::"
            "test_formal_build_manifest_rejects_missing_or_drifted_backend_identity",
            "tests/contracts/test_release_four_stage_handoff_contracts.py::"
            "test_promotion_rebuilds_from_tag_and_registry_plans_only_formal_artifacts",
            "tests/contracts/test_release_preview_contracts.py::"
            "test_releasable_history_generates_explained_version_and_isolated_artifacts",
        }
    ),
    "AC-089": frozenset(
        {
            "tests/contracts/test_typed_config_profiles_secret_files_contracts.py::"
            "test_local_and_service_profiles_load_typed_settings",
            "templates/service-app/tests/test_app_surface.py::"
            "test_api_docs_can_be_disabled_without_reading_assets",
        }
    ),
    "AC-096": frozenset(
        {
            "tests/contracts/test_agent_registry_schema_contracts.py::test_registry_exposes_compiled_definition_matching_public_descriptor_identity",
            "tests/contracts/test_agent_registry_schema_contracts.py::test_migrated_example_output_schemas_are_closed_and_reject_mixed_variants",
        }
    ),
    "AC-097": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_public_seam_contracts.py::test_bound_public_seam_repairs_once_and_persists_provider_neutral_result",
            "tests/contracts/test_provider_neutral_structured_adapter_contracts.py::test_adapter_returns_one_candidate_and_disables_sdk_retries",
        }
    ),
    "AC-098": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_schema_contracts.py::test_core_validator_rejects_extra_fields_and_never_returns_raw_candidate",
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_unknown_bound_schema_fails_before_usage_claim_or_provider_handle",
            "tests/contracts/test_provider_neutral_structured_cancellation_contracts.py::test_candidate_schema_identity_drift_maps_to_schema_invalid_and_exhausts_repair",
        }
    ),
    "AC-099": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_transport_contracts.py::test_missing_structured_protocol_fails_before_usage_claim",
            "tests/contracts/test_provider_neutral_structured_config_contracts.py::test_controlled_deployment_without_structured_capability_uses_stable_error",
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_any_explicit_fallback_route_is_rejected_before_claim_even_with_one_candidate",
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_repair_policy_overreach_fails_before_claim_or_provider_handle",
            "tests/contracts/test_provider_neutral_structured_budget_contracts.py::test_insufficient_direct_budget_rejects_before_provider_send",
        }
    ),
    "AC-100": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_budget_contracts.py::test_direct_structured_reserves_all_repair_requests_then_replaces_with_actual",
            "tests/contracts/test_provider_neutral_structured_price_identity_contracts.py::test_cost_disabled_structured_route_keeps_catalog_identity_and_completes",
            "tests/contracts/test_provider_neutral_structured_price_identity_contracts.py::test_incomplete_price_identity_is_mapped_at_public_structured_seam",
            "tests/contracts/test_provider_neutral_structured_mark_recovery_contracts.py::test_shared_budget_mark_commit_ack_unknown_keeps_reservation_and_needs_review",
            "tests/contracts/test_provider_neutral_structured_mark_recovery_contracts.py::test_allocation_mark_commit_ack_unknown_fences_parent_budget",
            "tests/contracts/test_provider_neutral_structured_transport_contracts.py::test_retryable_prepare_advances_transport_before_single_send",
            "tests/contracts/test_provider_neutral_structured_transport_contracts.py::test_retryable_prepare_exhaustion_is_bounded_and_never_sends",
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_repair_exhaustion_counts_every_request_and_never_crosses_provider",
        }
    ),
    "AC-101": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_sqlite_durable_structured_value_tamper_is_rejected_without_resend",
            "tests/contracts/test_provider_neutral_structured_cancellation_contracts.py::test_durable_started_after_crash_never_resends_or_fabricates_result",
            "tests/contracts/test_provider_neutral_structured_postgresql_contracts.py::test_postgresql_structured_success_repair_exact_replay_and_tamper_fence",
        }
    ),
    "AC-102": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_public_seam_contracts.py::test_bound_public_seam_repairs_once_and_persists_provider_neutral_result",
            "tests/contracts/test_provider_neutral_structured_cancellation_contracts.py::test_candidate_provider_drift_and_missing_usage_are_needs_review",
            "tests/contracts/test_provider_neutral_structured_eval_contracts.py::test_structured_eval_scores_only_valid_public_seam_result",
        }
    ),
    "AC-103": frozenset(
        {
            "tests/contracts/test_provider_neutral_structured_failure_contracts.py::test_any_explicit_fallback_route_is_rejected_before_claim_even_with_one_candidate",
            "tests/contracts/test_provider_neutral_structured_adapter_contracts.py::test_adapter_rejects_sdk_or_pydantic_output_without_stringifying_it",
        }
    ),
    "AC-104": frozenset(
        {
            "tests/contracts/test_provider_neutral_tool_catalog_contracts.py::test_provider_catalog_matches_frozen_golden_vector_byte_for_byte",
            "tests/contracts/test_tool_intent_usage_settlement_contracts.py::test_tool_intent_success_and_exact_replay_settle_usage_once",
            "tests/contracts/test_tool_intent_model_catalog_config_contracts.py::test_tool_intent_route_binds_actual_catalog_bytes_and_dynamic_budget",
        }
    ),
    "AC-105": frozenset(
        {
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_resolve_intent_fails_closed_without_execution_side_effects",
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_call_revalidates_resolved_intent_before_policy_or_handler",
            "tests/contracts/test_tool_registry_intent_resolution_contracts.py::test_call_approved_revalidates_before_claim_or_handler",
            "tests/contracts/test_tool_intent_policy_approval_contracts.py::test_synchronized_tool_recovery_tamper_fails_before_provider",
            "tests/contracts/test_policy_gated_model_tool_loop_policy_contracts.py::test_initial_loop_registry_drift_writes_redacted_validation_before_side_effects",
            "tests/contracts/test_policy_gated_model_tool_loop_approved_resume_recovery_contracts.py::test_approval_resume_registry_drift_writes_validation_before_tool_effects",
        }
    ),
    "AC-106": frozenset(
        {
            "tests/contracts/test_policy_gated_model_tool_loop_policy_contracts.py::test_real_policy_three_states_preserve_evidence_and_zero_execution",
            "tests/contracts/test_policy_gated_model_tool_loop_approval_public_seam_contracts.py::test_waiting_snapshot_resumes_through_call_approved_exactly_once",
            "tests/contracts/test_policy_gated_model_tool_loop_sqlite_resume_contracts.py::test_sqlite_reload_resumes_exact_snapshot_and_executes_tool_at_most_once",
        }
    ),
    "AC-107": frozenset(
        {
            "tests/contracts/test_policy_gated_model_tool_loop_result_guard_contracts.py::test_sensitive_success_is_redacted_before_context_reinjection",
            "tests/contracts/test_policy_gated_model_tool_loop_result_guard_contracts.py::test_truncated_success_reinjects_only_the_artifact_reference",
            "tests/contracts/test_policy_gated_model_tool_loop_event_contracts.py::test_full_loop_emits_linear_correlated_content_free_events",
        }
    ),
    "AC-108": frozenset(
        {
            "tests/contracts/test_policy_gated_tool_loop_registry_config_contracts.py::test_invalid_loop_shape_or_root_budget_fails_before_executor_import",
            "tests/contracts/test_policy_gated_tool_loop_registry_config_contracts.py::test_model_tool_loop_is_required_iff_route_supports_tool_intent",
            "tests/contracts/test_policy_gated_model_tool_loop_limits_contracts.py::test_limit_overrides_accept_null_inheritance_and_individual_shrinking",
            "tests/contracts/test_policy_gated_model_tool_loop_approval_public_seam_contracts.py::test_approval_resume_reuses_original_deadline_and_balance_before_handler",
        }
    ),
    "AC-109": frozenset(
        {
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
        }
    ),
    "AC-110": frozenset(
        {
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
        }
    ),
    "AC-111": frozenset(
        {
            "tests/contracts/test_model_tool_loop_migration_contracts.py::test_0018_sqlite_upgrade_preserves_legacy_tool_and_context_rows",
            "tests/contracts/test_model_tool_loop_marker_downgrade_contracts.py::test_tool_or_context_v1_evidence_commits_in_the_same_uow",
            "tests/contracts/test_model_tool_loop_postgresql_contracts.py::test_postgresql_repository_concurrency_cancel_and_budget_fencing",
            "tests/contracts/test_documentation_bilingual_contracts.py::test_deep_documentation_link_chain_is_bilingual",
            "tests/contracts/test_controlled_real_model_offline_contracts.py::test_default_gates_ignore_provider_credentials_and_network",
        }
    ),
}
