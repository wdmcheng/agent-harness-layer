"""共享预算 route-chain state、transition 与 attempt lifecycle 公共合同。"""

from __future__ import annotations

import importlib
from copy import deepcopy
from typing import Any

import pytest


def _state_type() -> type[Any]:
    """加载公开耐久状态 DTO；模块缺失即为实现前可解释 RED。"""

    module = importlib.import_module("agent_harness.storage.model_route_chain_state")
    return module.ModelRouteChainState


def _can_start_active_candidate(state: Any) -> bool:
    """调用恢复路径使用的公开判定函数，避免测试复制生产谓词。"""

    module = importlib.import_module("agent_harness.storage.model_route_chain_state")
    return bool(module.route_chain_can_start_active_candidate(state))


def _candidate(
    ordinal: int,
    *,
    state: str,
    side_effect_state: str = "not_started",
    reason: str | None = None,
) -> dict[str, Any]:
    """构造不含 provider 观察的候选基线，供状态矩阵按需覆盖。"""

    return {
        "ordinal": ordinal,
        "deployment_id": f"real-{ordinal}",
        "provider": "openai-compatible",
        "model": f"model-{ordinal}",
        "route_digest": f"{ordinal}" * 64,
        "state": state,
        "side_effect_state": side_effect_state,
        "reason": reason,
        "request_sent": False,
        "http_response_observed": False,
        "http_status": None,
        "response_identity_observed": False,
        "usage_observed": False,
        "text_observed": False,
        "delta_observed": False,
        "completion_observed": None,
        "not_started_proofs": [],
        "approval_request_binding_digest": None,
        "approval_grant_binding_digest": None,
    }


def _active_state() -> dict[str, Any]:
    """首候选已原子预约、尚未创建 provider attempt 的合法初始状态。"""

    return {
        "schema_version": "model-route-chain-state-v1",
        "chain_id": "a" * 64,
        "candidate_count": 2,
        "usage_call_id": "b" * 64,
        "operation_identity_digest": "c" * 64,
        "active_ordinal": 1,
        "waiting_approval_ordinal": None,
        "selected_ordinal": None,
        "evidence_route_ordinal": 1,
        "delta_fenced": False,
        "attempt_lifecycle": [],
        "current_reservation": {
            "candidate_ordinal": 1,
            "token_bound": 100,
            "cost_bound": 0.01,
        },
        "candidates": [
            _candidate(1, state="active"),
            _candidate(2, state="pending"),
        ],
        "transitions": [
            {
                "sequence": 1,
                "from_ordinal": None,
                "to_ordinal": 1,
                "state": "activated",
                "reason": "initial",
                "released_token_bound": 0,
                "released_cost_bound": None,
                "reserved_token_bound": 100,
                "reserved_cost_bound": 0.01,
            }
        ],
    }


def _client_proven_transfer_state() -> dict[str, Any]:
    """attempt 1 已由 client-not-started proof 原子关闭并转移到候选 2。"""

    state = _active_state()
    first = state["candidates"][0]
    first.update(
        {
            "state": "not_started",
            "reason": "client_not_started",
            "not_started_proofs": [
                {
                    "attempt": 1,
                    "reason": "client_not_started",
                    "side_effect_state": "not_started",
                    "request_sent": False,
                    "http_response_observed": False,
                    "http_status": None,
                    "response_identity_observed": False,
                    "usage_observed": False,
                    "text_observed": False,
                    "delta_observed": False,
                    "completion_observed": None,
                    "endpoint_policy_digest": "e" * 64,
                    "classifier_ref": None,
                    "classifier_version": None,
                    "proof_digest": "f" * 64,
                }
            ],
        }
    )
    state["candidates"][1]["state"] = "active"
    state["active_ordinal"] = 2
    state["evidence_route_ordinal"] = 2
    state["current_reservation"] = {
        "candidate_ordinal": 2,
        "token_bound": 80,
        "cost_bound": 0.02,
    }
    state["attempt_lifecycle"] = [
        {
            "attempt": 1,
            "candidate_ordinal": 1,
            "attempt_identity_digest": "d" * 64,
            "lifecycle_state": "not_started_proven",
            "side_effect_state": "not_started",
            "request_sent": False,
            "http_response_observed": False,
            "http_status": None,
            "response_identity_observed": False,
            "usage_observed": False,
            "text_observed": False,
            "delta_observed": False,
            "completion_observed": None,
            "not_started_proof_digest": "f" * 64,
        }
    ]
    state["transitions"].append(
        {
            "sequence": 2,
            "from_ordinal": 1,
            "to_ordinal": 2,
            "state": "transferred",
            "reason": "client_not_started",
            "released_token_bound": 100,
            "released_cost_bound": 0.01,
            "reserved_token_bound": 80,
            "reserved_cost_bound": 0.02,
        }
    )
    return state


def _cancelled_state() -> dict[str, Any]:
    """可信 stopped+complete 只形成空 selected、零 reservation 的取消终态。"""

    state = _active_state()
    state["active_ordinal"] = None
    state["selected_ordinal"] = None
    state["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    state["attempt_lifecycle"] = [
        {
            "attempt": 1,
            "candidate_ordinal": 1,
            "attempt_identity_digest": "d" * 64,
            "lifecycle_state": "settled",
            "side_effect_state": "result_committed",
            "request_sent": True,
            "http_response_observed": False,
            "http_status": None,
            "response_identity_observed": False,
            "usage_observed": True,
            "text_observed": False,
            "delta_observed": False,
            "completion_observed": False,
            "not_started_proof_digest": None,
        }
    ]
    state["candidates"][0].update(
        {
            "state": "cancelled",
            "side_effect_state": "result_committed",
            "reason": "invocation_cancelled",
            "request_sent": True,
            "usage_observed": True,
            "completion_observed": False,
        }
    )
    return state


def test_route_chain_active_and_proven_transfer_states_are_exact() -> None:
    """初始预约与 proof-close 后原子 transfer 两个 canonical 状态都可解析。"""

    state_type = _state_type()

    assert state_type.model_validate(_active_state()).to_payload() == _active_state()
    assert (
        state_type.model_validate(_client_proven_transfer_state()).to_payload()
        == _client_proven_transfer_state()
    )


def test_route_chain_cancelled_terminal_is_exact_and_rejects_success_shape_leaks() -> None:
    """取消终态不允许 selected、completed观察、delta或残留 reservation。"""

    state_type = _state_type()
    canonical = _cancelled_state()
    assert state_type.model_validate(canonical).to_payload() == canonical

    for mutate in (
        "selected",
        "reservation",
        "completion",
        "response_identity",
        "delta",
        "reason",
    ):
        invalid = deepcopy(canonical)
        lifecycle = invalid["attempt_lifecycle"][0]
        candidate = invalid["candidates"][0]
        if mutate == "selected":
            invalid["selected_ordinal"] = 1
        elif mutate == "reservation":
            invalid["current_reservation"] = {
                "candidate_ordinal": 1,
                "token_bound": 100,
                "cost_bound": 0.01,
            }
        elif mutate == "completion":
            lifecycle["completion_observed"] = True
            candidate["completion_observed"] = True
        elif mutate == "response_identity":
            lifecycle["response_identity_observed"] = True
            candidate["response_identity_observed"] = True
        elif mutate == "delta":
            lifecycle["delta_observed"] = True
            candidate["delta_observed"] = True
        else:
            candidate["reason"] = "provider_side_effect_unknown"
        with pytest.raises(ValueError):
            state_type.model_validate(invalid)


@pytest.mark.parametrize(
    ("mutate", "value"),
    [
        ("candidate_count_bool", True),
        ("duplicate_candidate", 1),
        ("active_and_waiting", 2),
        ("reservation_wrong_ordinal", 2),
        ("transition_gap", 3),
        ("illegal_reason", "approval_granted"),
        ("negative_bound", -1),
        ("nan_cost", float("nan")),
        ("unknown_field", True),
    ],
)
def test_route_chain_state_rejects_invalid_shape_and_transition_tuple(
    mutate: str,
    value: object,
) -> None:
    """状态、source anchor、transition 与数值 exact shape 不允许宽松解析。"""

    payload = _active_state()
    if mutate == "candidate_count_bool":
        payload["candidate_count"] = value
    elif mutate == "duplicate_candidate":
        payload["candidates"][1]["ordinal"] = value
    elif mutate == "active_and_waiting":
        payload["waiting_approval_ordinal"] = value
        payload["candidates"][1]["state"] = "waiting_approval"
    elif mutate == "reservation_wrong_ordinal":
        payload["current_reservation"]["candidate_ordinal"] = value
    elif mutate == "transition_gap":
        payload["transitions"][0]["sequence"] = value
    elif mutate == "illegal_reason":
        payload["transitions"][0]["reason"] = value
    elif mutate == "negative_bound":
        payload["current_reservation"]["token_bound"] = value
    elif mutate == "nan_cost":
        payload["current_reservation"]["cost_bound"] = value
    elif mutate == "unknown_field":
        payload["unknown"] = value

    with pytest.raises(ValueError):
        _state_type().model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        "attempt_gap",
        "duplicate_attempt",
        "proof_only_on_candidate",
        "proof_only_on_lifecycle",
        "started_with_proof",
        "unknown_with_proof",
        "identity_ordinal_drift",
    ],
)
def test_attempt_lifecycle_and_candidate_proofs_are_one_to_one(mutate: str) -> None:
    """全局 attempt 记录不可覆盖，proof 必须与同一 lifecycle 一一闭合。"""

    payload = _client_proven_transfer_state()
    lifecycle = payload["attempt_lifecycle"][0]
    if mutate == "attempt_gap":
        lifecycle["attempt"] = 2
    elif mutate == "duplicate_attempt":
        payload["attempt_lifecycle"].append(deepcopy(lifecycle))
    elif mutate == "proof_only_on_candidate":
        payload["attempt_lifecycle"] = []
    elif mutate == "proof_only_on_lifecycle":
        payload["candidates"][0]["not_started_proofs"] = []
    elif mutate == "started_with_proof":
        lifecycle["lifecycle_state"] = "started"
    elif mutate == "unknown_with_proof":
        lifecycle["lifecycle_state"] = "unknown"
        lifecycle["side_effect_state"] = "unknown"
    elif mutate == "identity_ordinal_drift":
        lifecycle["candidate_ordinal"] = 2

    with pytest.raises(ValueError):
        _state_type().model_validate(payload)


def test_completed_candidate_rejects_unknown_sibling_lifecycle() -> None:
    """终态候选只允许前序proof和唯一末尾settled，不能夹带unknown sibling。"""

    payload = _active_state()
    started = {
        "attempt": 1,
        "candidate_ordinal": 1,
        "attempt_identity_digest": "d" * 64,
        "lifecycle_state": "settled",
        "side_effect_state": "result_committed",
        "request_sent": False,
        "http_response_observed": False,
        "http_status": None,
        "response_identity_observed": False,
        "usage_observed": False,
        "text_observed": False,
        "delta_observed": False,
        "completion_observed": None,
        "not_started_proof_digest": None,
    }
    unknown = deepcopy(started)
    unknown.update(
        {
            "attempt": 2,
            "attempt_identity_digest": "9" * 64,
            "lifecycle_state": "unknown",
            "side_effect_state": "unknown",
        }
    )
    payload["attempt_lifecycle"] = [started, unknown]
    payload["active_ordinal"] = None
    payload["selected_ordinal"] = 1
    payload["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    payload["candidates"][0].update(
        {
            "state": "completed",
            "side_effect_state": "result_committed",
        }
    )

    with pytest.raises(ValueError):
        _state_type().model_validate(payload)


def test_active_candidate_can_resume_after_same_route_proof_commit() -> None:
    """同route proof已耐久且下一attempt identity尚不存在时允许安全恢复。"""

    payload = _client_proven_transfer_state()
    payload["active_ordinal"] = 1
    payload["evidence_route_ordinal"] = 1
    payload["current_reservation"] = {
        "candidate_ordinal": 1,
        "token_bound": 100,
        "cost_bound": 0.01,
    }
    payload["candidates"][0].update(
        {
            "state": "active",
            "reason": "client_not_started",
        }
    )
    payload["candidates"][1]["state"] = "pending"
    payload["transitions"] = payload["transitions"][:1]

    state = _state_type().model_validate(payload)

    assert _can_start_active_candidate(state) is True

    drifted = deepcopy(payload)
    drifted["transitions"][0]["reserved_token_bound"] = 101
    assert _can_start_active_candidate(_state_type().model_validate(drifted)) is False


def test_static_and_budget_skips_have_zero_attempt_and_no_transition() -> None:
    """初始普通 skip 只写候选状态，不建立 source anchor 或伪造 transition。"""

    payload = _active_state()
    payload["active_ordinal"] = None
    payload["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    payload["transitions"] = []
    payload["candidates"] = [
        _candidate(1, state="static_ineligible", reason="static_ineligible"),
        _candidate(2, state="budget_ineligible", reason="soft_budget"),
    ]

    parsed = _state_type().model_validate(payload)

    assert parsed.attempt_lifecycle == ()
    assert parsed.transitions == ()


def test_waiting_approval_and_canonical_approved_activation_are_distinct() -> None:
    """waiting 为零预约；获批只追加同 ordinal approved tuple，不能追加 activated。"""

    state_type = _state_type()
    waiting = _active_state()
    waiting["active_ordinal"] = None
    waiting["waiting_approval_ordinal"] = 1
    waiting["current_reservation"] = {
        "candidate_ordinal": None,
        "token_bound": 0,
        "cost_bound": None,
    }
    waiting["candidates"][0].update(
        {
            "state": "waiting_approval",
            "reason": "approval_required",
            "approval_request_binding_digest": "9" * 64,
        }
    )
    waiting["transitions"] = [
        {
            "sequence": 1,
            "from_ordinal": None,
            "to_ordinal": 1,
            "state": "waiting_approval",
            "reason": "approval_required",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 0,
            "reserved_cost_bound": None,
        }
    ]
    assert state_type.model_validate(waiting).waiting_approval_ordinal == 1

    approved = deepcopy(waiting)
    approved["active_ordinal"] = 1
    approved["waiting_approval_ordinal"] = None
    approved["current_reservation"] = {
        "candidate_ordinal": 1,
        "token_bound": 100,
        "cost_bound": 0.01,
    }
    approved["candidates"][0].update(
        {
            "state": "active",
            "reason": None,
            "approval_grant_binding_digest": "8" * 64,
        }
    )
    approved["transitions"].append(
        {
            "sequence": 2,
            "from_ordinal": 1,
            "to_ordinal": 1,
            "state": "approved",
            "reason": "approval_granted",
            "released_token_bound": 0,
            "released_cost_bound": None,
            "reserved_token_bound": 100,
            "reserved_cost_bound": 0.01,
        }
    )

    parsed = state_type.model_validate(approved)
    assert [item.state for item in parsed.transitions] == ["waiting_approval", "approved"]

    drifted_approved = deepcopy(approved)
    drifted_approved["transitions"][-1]["reserved_cost_bound"] = 0.02
    assert _can_start_active_candidate(state_type.model_validate(drifted_approved)) is False

    invalid = deepcopy(approved)
    duplicate_activation = deepcopy(invalid["transitions"][1])
    duplicate_activation.update(
        {
            "sequence": 3,
            "from_ordinal": None,
            "state": "activated",
            "reason": "initial",
        }
    )
    invalid["transitions"].append(duplicate_activation)
    with pytest.raises(ValueError):
        state_type.model_validate(invalid)
