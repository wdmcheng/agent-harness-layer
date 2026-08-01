"""受控多供应商回退的六组 route-chain canonical identity golden vectors。"""

from __future__ import annotations

import ast
import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.models import (
    ModelRouteApprovalGrantIdentity,
    ModelRouteApprovalRequestIdentity,
    ModelRouteAttemptIdentity,
    ModelRouteNotStartedProofIdentity,
    model_route_operation_identity_digest,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
ARGUMENTS = "1" * 64
APPROVAL_REQUEST_DIGEST = "802a004b444cdaf72c5dc0ad4a42bd71fc12f8a1e778b9e57745f2694a66ab82"


def test_route_chain_security_boundaries_do_not_escape_through_any() -> None:
    """事务回调与 canonical identity 输入必须保留可静态验证的窄类型。"""

    root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "packages/agent-harness/src/agent_harness/models/_streaming_consumption.py",
        "packages/agent-harness/src/agent_harness/models/_streaming_events.py",
        "packages/agent-harness/src/agent_harness/models/_router_identity.py",
        "packages/agent-harness/src/agent_harness/models/_route_chain_state_attempts.py",
        "packages/agent-harness/src/agent_harness/models/_invocation_planning.py",
    )
    escaped: list[str] = []
    for relative_path in relative_paths:
        tree = ast.parse((root / relative_path).read_text(encoding="utf-8"))
        if any(isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(tree)):
            escaped.append(relative_path)

    snapshot_path = "packages/agent-harness/src/agent_harness/models/_router_snapshot_chain.py"
    snapshot_tree = ast.parse((root / snapshot_path).read_text(encoding="utf-8"))
    selector = next(
        node
        for node in ast.walk(snapshot_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_select_snapshot_chain_refs"
    )
    if any(isinstance(node, ast.Name) and node.id == "Any" for node in ast.walk(selector)):
        escaped.append(f"{snapshot_path}::_select_snapshot_chain_refs")

    assert escaped == []


def test_route_chain_operation_identity_binds_original_operation_key_and_context() -> None:
    """审批前 identity 使用 U+001F exact fields，不能退化为 budget hash或approval id。"""

    digest = model_route_operation_identity_digest(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key="primary-model-call",
    )

    assert digest == "ec1b20d4f01dca07afa1aa02d2a828ebef6d055b4cefdbf52cfa4d1378d465de"
    assert digest != model_route_operation_identity_digest(
        tenant_id="tenant-a",
        run_id="run-a",
        agent_id="agent-a",
        request_id="request-a",
        trace_id="trace-a",
        operation_key="approved:approval-a",
    )


def _attempt() -> dict[str, object]:
    return {
        "schema_version": "model-route-attempt-identity-v1",
        "chain_id": A,
        "usage_call_id": B,
        "operation_identity_digest": C,
        "candidate_ordinal": 1,
        "global_attempt": 1,
        "route_digest": D,
        "endpoint_policy_digest": E,
        "retry_policy_digest": F,
    }


def _proof(*, trusted: bool) -> dict[str, object]:
    return {
        "schema_version": "model-route-not-started-proof-v1",
        "chain_id": A,
        "candidate_ordinal": 1,
        "global_attempt": 1,
        "reason": "trusted_business_not_started" if trusted else "client_not_started",
        "attempt_side_effect_state": "started" if trusted else "not_started",
        "request_sent": trusted,
        "http_response_observed": trusted,
        "http_status": 429 if trusted else None,
        "response_identity_observed": False,
        "usage_observed": False,
        "text_observed": False,
        "delta_observed": False,
        "completion_observed": False if trusted else None,
        "endpoint_policy_digest": E,
        "classifier_ref": "trusted_response_header_not_started" if trusted else None,
        "classifier_version": "v1" if trusted else None,
    }


def _approval_request(*, unicode_ref: bool = False) -> dict[str, object]:
    return {
        "schema_version": "model-route-chain-approval-request-v1",
        "chain_id": A,
        "candidate_ordinal": 1,
        "route_digest": D,
        "usage_call_id": B,
        "operation_identity_digest": C,
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "agent_id": "agent-a",
        "request_id": "request-a",
        "trace_id": "trace-a",
        "action": "model.invoke",
        "resource": "agent:agent-a:model",
        "arguments_ref": "artifact://arguments-a/参数" if unicode_ref else "artifact://arguments-a",
        "arguments_hash": ARGUMENTS,
    }


def _approval_grant() -> dict[str, object]:
    return {
        "schema_version": "model-route-chain-approval-grant-v1",
        "request_binding_digest": APPROVAL_REQUEST_DIGEST,
        "usage_call_id": B,
        "operation_identity_digest": C,
        "approval_id": "approval-a",
        "lease_id": "lease-a",
        "tenant_id": "tenant-a",
        "identity_id": "identity-a",
        "agent_id": "agent-a",
        "run_id": "run-a",
        "action": "model.invoke",
        "resource": "agent:agent-a:model",
        "arguments_hash": ARGUMENTS,
    }


@pytest.mark.parametrize(
    ("identity_type", "payload", "expected"),
    [
        (
            ModelRouteAttemptIdentity,
            _attempt(),
            "d5591241b4786cb8142642e58f7b7e295f46a1ed0c0ea2a8599bfa4a3f0eaa21",
        ),
        (
            ModelRouteNotStartedProofIdentity,
            _proof(trusted=False),
            "9acc29f454c47d773bb692ae5046b97b00bfc218f273b10a7399f2b18bd6fb5b",
        ),
        (
            ModelRouteNotStartedProofIdentity,
            _proof(trusted=True),
            "fe2a4837c90958ca36427e6f7cd7b088bb2361a78515b4bdbedd3ceeb1c0a8c0",
        ),
        (
            ModelRouteApprovalRequestIdentity,
            _approval_request(),
            "802a004b444cdaf72c5dc0ad4a42bd71fc12f8a1e778b9e57745f2694a66ab82",
        ),
        (
            ModelRouteApprovalRequestIdentity,
            _approval_request(unicode_ref=True),
            "20dfca2bea60ee5a5cf4565a339a13eafcbe2de2f52fc41b95ab91a9445c4297",
        ),
        (
            ModelRouteApprovalGrantIdentity,
            _approval_grant(),
            "d743ba666b06b5ce289da94503f8d39f85d9eb0da57fc058b7f1c085f9c0b782",
        ),
    ],
)
def test_route_chain_identity_golden_vectors(
    identity_type: type[ModelRouteAttemptIdentity]
    | type[ModelRouteNotStartedProofIdentity]
    | type[ModelRouteApprovalRequestIdentity]
    | type[ModelRouteApprovalGrantIdentity],
    payload: dict[str, object],
    expected: str,
) -> None:
    """六组 active delta 字节与摘要由同一公共 DTO 逐值复算。"""

    identity = identity_type.model_validate(payload)

    assert identity.digest() == expected
    assert identity.canonical_bytes() == json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def test_unicode_approval_binding_rejects_escaped_ascii_digest() -> None:
    """`参数` 保持原文 UTF-8；ensure_ascii=true 的已知错误摘要绝不能命中。"""

    identity = ModelRouteApprovalRequestIdentity.model_validate(_approval_request(unicode_ref=True))
    escaped = hashlib.sha256(
        json.dumps(
            identity.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert escaped == "bc72de9dea927cc0e34afad8667f682b16d8f76eb2b105f9c0c0bdaf13579864"
    assert identity.digest() != escaped
    assert "参数".encode() in identity.canonical_bytes()


@pytest.mark.parametrize("mutation", ["unknown", "missing", "bool_ordinal", "null_omitted"])
def test_route_chain_identity_exact_shape_rejects_drift(mutation: str) -> None:
    """unknown、缺失、bool 数字与 nullable 省略都在摘要前关闭失败。"""

    payload = deepcopy(_approval_request())
    if mutation == "unknown":
        payload["unknown"] = True
    elif mutation == "missing":
        del payload["chain_id"]
    elif mutation == "bool_ordinal":
        payload["candidate_ordinal"] = True
    else:
        del payload["request_id"]

    with pytest.raises(ValidationError):
        ModelRouteApprovalRequestIdentity.model_validate(payload)
