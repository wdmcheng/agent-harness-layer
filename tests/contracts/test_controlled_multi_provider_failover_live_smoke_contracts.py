"""多供应商 failover live smoke 四分支 artifact 公共合同。"""

from __future__ import annotations

import importlib
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from scripts.smoke_live_model import LiveSmokeExecutor
from tests.contracts.controlled_multi_provider_failover_test_support import (
    ROUTE_A,
    ROUTE_B,
    bound_failover_invocation,
)

from agent_harness.identity import IdentityContext
from agent_harness.models import ModelRequest
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest, RunStatus

ROOT = Path(__file__).resolve().parents[2]


def _contract_module() -> Any:
    """从提交脚本加载唯一 validator；缺少模块本身就是实现前 RED。"""

    return importlib.import_module("scripts.live_model_failover_contract")


def _passed_payload() -> dict[str, Any]:
    """构造同 provider kind、不同 deployment 的唯一 PASS 基准形状。"""

    return {
        "schema_version": "model-failover-live-smoke/v1",
        "status": "passed",
        "provider_called": True,
        "attempt_count": 2,
        "chain_id": "a" * 64,
        "selected_ordinal": 2,
        "candidates": [
            {
                "ordinal": 1,
                "deployment_id": "real_primary",
                "provider": "openai-compatible",
                "model": "fixture-text-1",
                "outcome": "not_started",
                "attempt_count": 1,
                "not_started_proof_count": 1,
                "request_sent": False,
                "response_observed": False,
                "not_started_reason": "client_not_started",
                "http_status": None,
            },
            {
                "ordinal": 2,
                "deployment_id": "real_secondary",
                "provider": "openai-compatible",
                "model": "fixture-text-2",
                "outcome": "completed",
                "attempt_count": 1,
                "not_started_proof_count": 0,
                "request_sent": True,
                "response_observed": True,
                "not_started_reason": None,
                "http_status": 200,
            },
        ],
        "usage": {
            "input_tokens": 3,
            "output_tokens": 5,
            "cost_usd": 0.0002,
            "cost_status": "reported",
        },
        "reason_code": None,
    }


def _hosted_payload(reason: str) -> dict[str, Any]:
    """所有未满足前置都必须返回相同的零调用空 identity 形状。"""

    return {
        "schema_version": "model-failover-live-smoke/v1",
        "status": "hosted-unverified",
        "provider_called": False,
        "attempt_count": 0,
        "chain_id": None,
        "selected_ordinal": None,
        "candidates": [],
        "usage": None,
        "reason_code": reason,
    }


def _preflight_routes() -> list[dict[str, object]]:
    """两个 route 共用 provider kind，但 deployment、凭据与 endpoint 完全隔离。"""

    return [
        {
            "deployment_id": "real_primary",
            "provider_kind": "openai-compatible",
            "credential_ref": "real_primary_key",
            "endpoint_origin": "https://models-primary.example.test",
            "max_attempts": 1,
        },
        {
            "deployment_id": "real_secondary",
            "provider_kind": "openai-compatible",
            "credential_ref": "real_secondary_key",
            "endpoint_origin": "https://models-secondary.example.test",
            "max_attempts": 1,
        },
    ]


def test_failover_live_preflight_reason_priority_is_zero_call() -> None:
    """前置缺失按固定优先级收敛，且 validator 只接受零调用空 identity。"""

    contract = _contract_module()
    cases: list[tuple[dict[str, bool], str]] = [
        ({}, "authorization_missing"),
        ({"authorized": True}, "failover_opt_in_missing"),
        (
            {"authorized": True, "failover_opt_in": True},
            "credential_pair_missing",
        ),
        (
            {
                "authorized": True,
                "failover_opt_in": True,
                "credential_pair_present": True,
            },
            "deployment_pair_invalid",
        ),
        (
            {
                "authorized": True,
                "failover_opt_in": True,
                "credential_pair_present": True,
                "deployment_pair_valid": True,
            },
            "not_started_fixture_missing",
        ),
    ]

    for preflight, reason in cases:
        result = contract.preflight_result(**preflight)
        assert contract.validate_result(result) == _hosted_payload(reason)


def test_same_kind_two_deployment_artifact_can_pass() -> None:
    """隔离依据是 deployment/credential/endpoint，不得错误要求 provider kind 不同。"""

    contract = _contract_module()

    assert contract.validate_preflight_routes(_preflight_routes()) == _preflight_routes()
    payload = _passed_payload()
    assert contract.validate_result(payload) == payload


@pytest.mark.parametrize("field", ["deployment_id", "credential_ref", "endpoint_origin"])
def test_reused_deployment_credential_or_endpoint_is_rejected(field: str) -> None:
    """任一安全 identity 复用都不能进入真实调用。"""

    contract = _contract_module()
    routes = _preflight_routes()
    routes[1][field] = routes[0][field]

    with pytest.raises(ValueError):
        contract.validate_preflight_routes(routes)


@pytest.mark.parametrize(
    ("mutate", "value"),
    [
        ("top_unknown", True),
        ("duplicate_ordinal", 1),
        ("zero_ordinal", 0),
        ("selected_missing", None),
        ("selected_unknown", 3),
        ("duplicate_completed", "completed"),
        ("fractional_input", 1.5),
        ("bool_output", True),
        ("negative_output", -1),
        ("invalid_cost_status", "partial"),
        ("null_reported_cost", None),
        ("proof_count_mismatch", 0),
        ("attempt_count_mismatch", 3),
        ("trusted_without_response", False),
        ("passed_reason", "contract_failure"),
    ],
)
def test_failover_live_validator_rejects_invalid_attempt_proof_usage_unions(
    mutate: str,
    value: object,
) -> None:
    """数字、ordinal、proof、usage 与 status/reason 的封闭联合逐项拒绝漂移。"""

    contract = _contract_module()
    payload = _passed_payload()
    if mutate == "top_unknown":
        payload["unknown"] = value
    elif mutate == "duplicate_ordinal":
        payload["candidates"][1]["ordinal"] = value
    elif mutate == "zero_ordinal":
        payload["candidates"][0]["ordinal"] = value
    elif mutate == "selected_missing":
        del payload["selected_ordinal"]
    elif mutate == "selected_unknown":
        payload["selected_ordinal"] = value
    elif mutate == "duplicate_completed":
        payload["candidates"][0]["outcome"] = value
        payload["candidates"][0]["not_started_proof_count"] = 0
        payload["candidates"][0]["not_started_reason"] = None
    elif mutate == "fractional_input":
        payload["usage"]["input_tokens"] = value
    elif mutate == "bool_output":
        payload["usage"]["output_tokens"] = value
    elif mutate == "negative_output":
        payload["usage"]["output_tokens"] = value
    elif mutate == "invalid_cost_status":
        payload["usage"]["cost_status"] = value
    elif mutate == "null_reported_cost":
        payload["usage"]["cost_usd"] = value
    elif mutate == "proof_count_mismatch":
        payload["candidates"][0]["not_started_proof_count"] = value
    elif mutate == "attempt_count_mismatch":
        payload["attempt_count"] = value
    elif mutate == "trusted_without_response":
        first = payload["candidates"][0]
        first.update(
            {
                "request_sent": True,
                "response_observed": value,
                "not_started_reason": "trusted_business_not_started",
                "http_status": 429,
            }
        )
    elif mutate == "passed_reason":
        payload["reason_code"] = value

    with pytest.raises(ValueError):
        contract.validate_result(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _hosted_payload("authorization_missing") | {"provider_called": True},
        _hosted_payload("authorization_missing") | {"chain_id": "a" * 64},
        _passed_payload()
        | {
            "status": "external-blocked",
            "selected_ordinal": 2,
            "reason_code": "network_unavailable",
        },
        _passed_payload()
        | {
            "status": "failed",
            "chain_id": None,
            "reason_code": "contract_failure",
        },
    ],
)
def test_failover_live_validator_rejects_mixed_status_identity_shapes(
    payload: dict[str, Any],
) -> None:
    """hosted、冻结前失败与冻结后失败不得互相拼接字段。"""

    with pytest.raises(ValueError):
        _contract_module().validate_result(deepcopy(payload))


def test_passed_artifact_matches_durable_chain_attempt_and_usage_evidence() -> None:
    """PASS 不能由脚本自报，必须逐值命中同一耐久 chain、attempt、proof 与 usage。"""

    contract = _contract_module()
    payload = _passed_payload()
    evidence = {
        "chain_id": payload["chain_id"],
        "selected_ordinal": 2,
        "candidates": deepcopy(payload["candidates"]),
        "attempts": [
            {"attempt": 1, "candidate_ordinal": 1, "not_started_proof_count": 1},
            {"attempt": 2, "candidate_ordinal": 2, "not_started_proof_count": 0},
        ],
        "usage": deepcopy(payload["usage"]),
    }

    assert contract.validate_result_against_evidence(payload, evidence) == payload

    for field, invalid in [("chain_id", "b" * 64), ("selected_ordinal", 1)]:
        drifted = deepcopy(evidence)
        drifted[field] = invalid
        with pytest.raises(ValueError):
            contract.validate_result_against_evidence(payload, drifted)


@pytest.mark.asyncio
async def test_failover_live_producer_reads_durable_usage_evidence(tmp_path: Path) -> None:
    """PASS producer 必须从 usage outbox 读取结算值并绑定同一耐久 chain。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        response = await fixture.bound.complete(
            ModelRequest(prompt="live evidence", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )

        producer = importlib.import_module("scripts.smoke_live_model_failover")
        state, durable = await producer.load_durable_failover_evidence(
            storage=fixture.storage,
            tenant_id="tenant-a",
            run_id=fixture.run_id,
            usage_call_id=fixture.usage_call_id,
        )

        assert durable["chain_id"] == state.chain_id
        assert durable["selected_ordinal"] == 2
        assert durable["usage"] == {
            "input_tokens": response.token_usage["input_tokens"],
            "output_tokens": response.token_usage["output_tokens"],
            "cost_usd": response.cost_usd,
            "cost_status": response.cost_status,
        }
    finally:
        await fixture.storage.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "chain_schema",
        "final_chain_identity",
        "started_chain_identity",
        "route",
        "attempt",
        "proof",
        "budget_charge",
    ],
)
@pytest.mark.asyncio
async def test_failover_live_producer_rejects_tampered_durable_chain_identity(
    tmp_path: Path,
    mutation: str,
) -> None:
    """PASS producer 必须完整校验 outbox 的 started/final settlement envelope。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["client_not_started"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    try:
        await fixture.bound.complete(
            ModelRequest(prompt="tampered live evidence", max_output_tokens=8),
            operation_key=fixture.operation_key,
        )
        async with fixture.storage.uow() as uow:
            usage_record = await uow.evidence_outbox.get_usage(
                tenant_id="tenant-a",
                usage_call_id=fixture.usage_call_id,
            )
            assert usage_record.result_json is not None
            tampered = deepcopy(usage_record.result_json)
            final_decision = tampered["evidence"]["decision"]
            started_decision = tampered["started"]["decision"]
            if mutation == "chain_schema":
                final_decision["route_chain"]["schema_version"] = "tampered"
            elif mutation == "final_chain_identity":
                final_decision["route_chain"]["identity"] = {"schema_version": "tampered"}
            elif mutation == "started_chain_identity":
                started_decision["route_chain"]["identity"] = {"schema_version": "tampered"}
            elif mutation == "route":
                final_decision["route"]["deployment_id"] = "tampered"
            elif mutation == "attempt":
                final_decision["attempts"][0]["candidate_ordinal"] = 2
            elif mutation == "proof":
                final_decision["attempts"][0]["not_started_proof_digest"] = "0" * 64
            elif mutation == "budget_charge":
                final_decision["budget_charge"]["charged_tokens"] = 0
            else:  # pragma: no cover - 参数表是封闭测试输入。
                raise AssertionError(f"unsupported mutation: {mutation}")
            usage_record.result_json = tampered
            await uow.commit()

        producer = importlib.import_module("scripts.smoke_live_model_failover")
        with pytest.raises(ValueError, match="usage evidence"):
            await producer.load_durable_failover_evidence(
                storage=fixture.storage,
                tenant_id="tenant-a",
                run_id=fixture.run_id,
                usage_call_id=fixture.usage_call_id,
            )
    finally:
        await fixture.storage.dispose()


def test_frozen_contract_failure_keeps_chain_and_unknown_attempt_facts() -> None:
    """链冻结后的本地失败必须保留 identity/attempt，不能伪装成零调用。"""

    payload = _passed_payload()
    payload.update(
        {
            "status": "failed",
            "selected_ordinal": None,
            "usage": None,
            "reason_code": "contract_failure",
        }
    )
    payload["candidates"][1].update(
        {
            "outcome": "unknown",
            "not_started_proof_count": 0,
            "not_started_reason": None,
        }
    )

    assert _contract_module().validate_result(payload) == payload


@pytest.mark.asyncio
async def test_failover_live_executor_preserves_runtime_failure_domain(tmp_path: Path) -> None:
    """真实bound cleanup失败必须越过executor保持本地运行时归因。"""

    fixture = await bound_failover_invocation(
        tmp_path,
        route_count=2,
        scripts={
            ROUTE_A["deployment_id"]: ["completed"],
            ROUTE_B["deployment_id"]: ["completed"],
        },
    )
    fixture.provider.cleanup_failures.add(ROUTE_A["deployment_id"])

    executor = LiveSmokeExecutor(
        ModelRequest(
            prompt="fixed failover smoke prompt",
            max_output_tokens=8,
        )
    )
    context = AgentExecutionContext(identity=IdentityContext.local_default()).bind_services(
        {"model_invocation": fixture.bound}
    )
    try:
        result = await executor.run(
            AgentExecutionRequest(
                agent_id="system.live_model_failover_smoke",
                run_id=fixture.run_id,
                input={},
            ),
            context,
        )
    finally:
        await fixture.storage.dispose()

    assert result.status == RunStatus.FAILED.value
    assert executor.error_code == "model.provider_side_effect_unknown"
    assert executor.failure_domain == "runtime"
    assert executor.provider_called is True
    assert executor.attempt_count == 1


@pytest.mark.parametrize(
    ("failure_domain", "error_code", "expected_status", "expected_reason", "exit_code"),
    [
        ("runtime", "model.provider_failed", "failed", "contract_failure", 1),
        (None, None, "failed", "contract_failure", 1),
        ("provider", "model.provider_failed", "external-blocked", "provider_rejected", 2),
    ],
)
def test_frozen_failover_failure_classification_requires_explicit_provider_domain(
    failure_domain: str | None,
    error_code: str | None,
    expected_status: str,
    expected_reason: str,
    exit_code: int,
) -> None:
    """仅显式 provider 故障可报告外部阻断，本地失败仍保留冻结耐久事实。"""

    failover_smoke = importlib.import_module("scripts.smoke_live_model_failover")
    classify = failover_smoke.classify_frozen_run_failure
    baseline = _passed_payload()
    candidates = deepcopy(baseline["candidates"])
    candidates[1].update(
        {
            "outcome": "unknown",
            "not_started_proof_count": 0,
            "not_started_reason": None,
        }
    )

    payload, actual_exit_code = classify(
        chain_id=baseline["chain_id"],
        selected_ordinal=None,
        candidates=candidates,
        provider_called=True,
        attempt_count=2,
        error_code=error_code,
        failure_domain=failure_domain,
    )

    assert actual_exit_code == exit_code
    assert payload["status"] == expected_status
    assert payload["reason_code"] == expected_reason
    assert payload["chain_id"] == baseline["chain_id"]
    assert payload["selected_ordinal"] is None
    assert payload["candidates"] == candidates
    assert payload["provider_called"] is True
    assert payload["attempt_count"] == 2


def test_failover_live_make_and_ci_targets_are_declared() -> None:
    """本地 producer 与 CI wrapper 必须是独立显式入口，默认门禁不触发 live 调用。"""

    makefile = ROOT.joinpath("Makefile").read_text(encoding="utf-8")

    assert "smoke-live-model-failover:" in makefile
    assert "ci-smoke-live-model-failover:" in makefile
    assert "scripts/smoke_live_model_failover.py" in makefile
