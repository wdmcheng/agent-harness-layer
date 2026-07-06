from __future__ import annotations

from pydantic import ValidationError

from agent_harness.contracts import (
    ApiErrorEnvelope,
    ContextInput,
    ContextRef,
    ErrorDetail,
    GuardrailDecision,
    GuardrailDecisionStatus,
    HarnessDTO,
    SourceRef,
    TrustLevel,
)
from agent_harness.identity import IdentityContext, PermissionContext


class ExampleDTO(HarnessDTO):
    run_id: str


def test_dto_serializes_json_payload_and_rejects_unknown_fields() -> None:
    payload = ExampleDTO(run_id="run-1").to_payload()

    assert payload == {"run_id": "run-1"}

    try:
        ExampleDTO.model_validate({"run_id": "run-1", "vendor_object": object()})
    except ValidationError as exc:
        assert exc.errors()[0]["loc"] == ("vendor_object",)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unknown DTO fields must fail validation")


def test_error_envelope_exposes_field_path_and_repair_hint() -> None:
    envelope = ApiErrorEnvelope(
        error=ErrorDetail(
            code="config.missing",
            message="缺少必填配置",
            field_path="storage.kind",
            hint="在 profile YAML 中设置 storage.kind",
        )
    )

    assert envelope.to_payload()["error"]["field_path"] == "storage.kind"
    assert envelope.to_payload()["error"]["hint"].startswith("在 profile YAML")


def test_trust_context_and_guardrail_decision_are_serializable() -> None:
    source = SourceRef(kind="tool", uri="mcp://search", label="Search tool")
    context_ref = ContextRef(
        context_id="ctx-1",
        source_ref=source,
        trust_level=TrustLevel.UNTRUSTED,
        truncated=True,
        token_count=128,
    )
    context = ContextInput(
        content="外部工具输出",
        refs=[context_ref],
        trust_level=TrustLevel.UNTRUSTED,
    )
    decision = GuardrailDecision.require_approval(
        reason="shell action requires approval",
        metadata={"approval_scope": "shell"},
    )

    context_payload = context.to_payload()
    decision_payload = decision.to_payload()

    assert context_payload["refs"][0]["source_ref"]["kind"] == "tool"
    assert context_payload["refs"][0]["trust_level"] == "untrusted"
    assert context_payload["refs"][0]["truncated"] is True
    assert decision.status is GuardrailDecisionStatus.REQUIRE_APPROVAL
    assert decision_payload["status"] == "require_approval"
    assert decision_payload["metadata"]["approval_scope"] == "shell"


def test_identity_and_permission_context_keep_tenant_session_fields() -> None:
    identity = IdentityContext.local_default(session_id="session-1")
    permission = PermissionContext.from_identity(
        identity,
        agent_id="agent-1",
        resource="shell",
        action="execute",
    )

    assert identity.tenant_id == "default"
    assert identity.user_id == "local-user"
    assert identity.session_id == "session-1"
    assert permission.tenant_id == "default"
    assert permission.agent_id == "agent-1"
    assert permission.to_payload()["permissions"] == ["*"]
