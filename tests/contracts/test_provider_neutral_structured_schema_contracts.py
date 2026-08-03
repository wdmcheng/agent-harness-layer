"""结构化schema identity与provider-neutral DTO的公开合同。"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from agent_harness.models import (
    ModelAttemptEvidence,
    OutputSchemaIdentity,
    StructuredOutputNotStartedProof,
    StructuredOutputReplayIdentity,
    StructuredOutputRequest,
    StructuredProviderCandidate,
    canonical_structured_json,
    compile_output_schema,
    compile_output_schema_definition,
    validate_structured_candidate,
)


class _NestedPayload(BaseModel):
    """嵌套对象用于证明 compiler 会递归关闭额外字段。"""

    model_config = ConfigDict(extra="forbid")

    count: int


class _StructuredOutput(BaseModel):
    """公开 structured seam 的最小成功 schema。"""

    model_config = ConfigDict(extra="forbid")

    answer: str
    nested: _NestedPayload


def test_schema_compiler_produces_stable_identity_and_closed_nested_objects() -> None:
    """相同 ref/version/schema 必须产生稳定 canonical bytes 与 SHA-256。"""

    first = compile_output_schema(
        _StructuredOutput,
        schema_ref="agents.example.schemas.StructuredOutput",
        version="1.0.0",
    )
    second = compile_output_schema(
        _StructuredOutput,
        schema_ref="agents.example.schemas.StructuredOutput",
        version="1.0.0",
    )

    assert first == second
    assert first.identity == OutputSchemaIdentity(
        schema_ref="agents.example.schemas.StructuredOutput",
        version="1.0.0",
        digest=hashlib.sha256(first.canonical_schema.encode("utf-8")).hexdigest(),
    )
    assert first.schema_definition["additionalProperties"] is False
    assert first.schema_definition["properties"]["nested"]["additionalProperties"] is False
    assert "$ref" not in first.canonical_schema


def test_core_validator_rejects_extra_fields_and_never_returns_raw_candidate() -> None:
    """额外字段只能形成稳定 issue，不得因 Pydantic 默认行为被忽略。"""

    schema = compile_output_schema(
        _StructuredOutput,
        schema_ref="agents.example.schemas.StructuredOutput",
        version="1.0.0",
    )
    candidate = StructuredProviderCandidate(
        schema_identity=schema.identity,
        provider="provider-a",
        model="model-a",
        candidate={"answer": "ok", "nested": {"count": 1, "secret": "drop"}},
        attempts=[
            ModelAttemptEvidence(
                attempt=1,
                side_effect_state="started",
                outcome="completed",
                completion_observed=True,
                input_tokens=2,
                output_tokens=3,
                cost_status="unavailable",
                latency_ms=1,
            )
        ],
    )

    with pytest.raises(ValidationError, match="frozen_instance"):
        candidate.provider = "provider-b"

    result = validate_structured_candidate(candidate, schema=schema)

    assert result.value is None
    assert result.status == "extra_fields"
    assert result.issues == [{"code": "extra_field", "path": "/nested/secret"}]
    assert "drop" not in result.model_dump_json()


def test_nullable_object_schema_is_recursively_closed_and_rejects_nested_extra() -> None:
    """`object|null`仍是对象授权边界，不能让nullable写法绕过额外字段拒绝。"""

    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {
                "payload": {
                    "type": ["object", "null"],
                }
            },
            "required": ["payload"],
        },
        schema_ref="fixture.NullableObject",
        version="v1",
    )
    candidate = StructuredProviderCandidate(
        schema_identity=schema.identity,
        provider="provider-a",
        model="model-a",
        candidate={"payload": {"unexpected": 1}},
        attempts=[
            ModelAttemptEvidence(
                attempt=1,
                side_effect_state="started",
                outcome="completed",
                completion_observed=True,
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
            )
        ],
    )

    assert schema.schema_definition["properties"]["payload"]["additionalProperties"] is False
    result = validate_structured_candidate(candidate, schema=schema)
    assert result.status == "extra_fields"
    assert result.value is None
    assert result.issues == [{"code": "extra_field", "path": "/payload/unexpected"}]


def test_core_validator_maps_and_sorts_only_frozen_issue_vocabulary() -> None:
    """Type、enum、constraint、额外字段与非法 JSON 只形成稳定 code/path 摘要。"""

    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1},
                "kind": {"type": "string", "enum": ["ok"]},
            },
            "required": ["count", "kind"],
            "additionalProperties": False,
        },
        schema_ref="fixture.IssueVocabulary",
        version="v1",
    )

    def candidate(value: str | dict[str, object]) -> StructuredProviderCandidate:
        """每个验证分支复用同一个已完成、计量完整的 provider-local attempt。"""

        return StructuredProviderCandidate(
            schema_identity=schema.identity,
            provider="provider-a",
            model="model-a",
            candidate=value,
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="completed",
                    completion_observed=True,
                    input_tokens=1,
                    output_tokens=1,
                    latency_ms=1,
                )
            ],
        )

    mixed = validate_structured_candidate(
        candidate({"count": "bad", "kind": "no", "extra": True}),
        schema=schema,
    )
    assert mixed.issues == [
        {"code": "type_mismatch", "path": "/count"},
        {"code": "extra_field", "path": "/extra"},
        {"code": "value_not_allowed", "path": "/kind"},
    ]
    constrained = validate_structured_candidate(
        candidate({"count": 0, "kind": "ok"}),
        schema=schema,
    )
    assert constrained.issues == [{"code": "constraint_violation", "path": "/count"}]
    malformed = validate_structured_candidate(candidate("{"), schema=schema)
    assert malformed.issues == [{"code": "json_invalid", "path": ""}]


def test_structured_canonical_json_matches_frozen_replay_vector() -> None:
    """Replay 预映像使用唯一 serializer，nullable 字段不可被省略。"""

    payload: dict[str, object] = {
        "schema_version": "structured-output-replay-v1",
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "agent_id": "agent-a",
        "request_id": None,
        "trace_id": "trace-a",
        "usage_call_id": "a" * 64,
        "operation_identity_digest": "b" * 64,
        "prompt_digest": "c" * 64,
        "deployment_id": "deployment-a",
        "provider": "provider-a",
        "model": "model-a",
        "route_digest": "d" * 64,
        "schema_identity": {
            "schema_version": "output-schema-identity-v1",
            "schema_ref": "agents.example.schemas:Output",
            "version": "1.0.0",
            "digest": "e" * 64,
        },
        "transport_attempt_limit": 1,
        "repair_limit": 1,
        "repair_count": 1,
        "provider_request_count": 2,
        "final_status": "valid",
        "value_digest": "f" * 64,
    }

    encoded = canonical_structured_json(payload)

    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() == (
        "6c3ef8f5d9444b1e70796996344af02033b29810b7fb6537b483e8ea230ff819"
    )


@pytest.mark.parametrize("field", ["request_id", "trace_id"])
def test_replay_identity_rejects_empty_nullable_identity_fields(field: str) -> None:
    """Nullable身份只接受非空字符串或null，空串不能形成第二种exact identity。"""

    payload: dict[str, object] = {
        "tenant_id": "tenant-a",
        "run_id": "run-a",
        "agent_id": "agent-a",
        "request_id": "request-a",
        "trace_id": "trace-a",
        "usage_call_id": "a" * 64,
        "operation_identity_digest": "b" * 64,
        "prompt_digest": "c" * 64,
        "deployment_id": "deployment-a",
        "provider": "provider-a",
        "model": "model-a",
        "route_digest": "d" * 64,
        "schema_identity": OutputSchemaIdentity(
            schema_ref="agents.example.schemas:Output",
            version="1.0.0",
            digest="e" * 64,
        ),
        "transport_attempt_limit": 1,
        "repair_limit": 0,
        "repair_count": 0,
        "provider_request_count": 0,
        "final_status": "failed",
        "value_digest": None,
    }
    payload[field] = ""
    with pytest.raises(ValueError):
        StructuredOutputReplayIdentity.model_validate(payload)

    payload[field] = None
    assert getattr(StructuredOutputReplayIdentity.model_validate(payload), field) is None


def test_not_started_proof_matches_frozen_golden_vector_and_rejects_tamper() -> None:
    """完整 proof 必须逐字节复算契约 golden preimage，不能只校验 digest 外形。"""

    payload = {
        "schema_version": "structured-output-not-started-proof-v1",
        "kind": "client_prepare_not_started",
        "usage_call_id": "a" * 64,
        "operation_identity_digest": "b" * 64,
        "route_digest": "d" * 64,
        "schema_identity": {
            "schema_version": "output-schema-identity-v1",
            "schema_ref": "agents.example.schemas:Output",
            "version": "1.0.0",
            "digest": "e" * 64,
        },
        "prompt_digest": "c" * 64,
        "attempt": 1,
        "repair_ordinal": 0,
        "transport_ordinal": 1,
        "digest": "940380a8980e64c418b0836366a34520b41e62688610b3e25a288e89940a5d2b",
    }

    proof = StructuredOutputNotStartedProof.model_validate(payload)
    assert proof.digest == payload["digest"]
    tampered = dict(payload)
    tampered["transport_ordinal"] = 2
    with pytest.raises(ValueError):
        StructuredOutputNotStartedProof.model_validate(tampered)


def test_structured_output_request_serializes_exact_schema_and_repair_policy() -> None:
    """Semantic request 只承载 provider-neutral schema definition 与有限 repair 上限。"""

    schema = compile_output_schema(
        _StructuredOutput,
        schema_ref="agents.example.schemas.StructuredOutput",
        version="1.0.0",
    )
    request = StructuredOutputRequest(schema=schema.identity, repair_limit=1)

    assert set(request.to_payload()) == {"schema", "repair_limit"}
    assert request.to_payload()["schema"] == schema.identity.to_payload()


def test_candidate_requires_one_local_attempt_and_rejects_duplicate_metering() -> None:
    """Candidate 顶层不得出现第二份 token/cost/latency 真相源。"""

    identity = OutputSchemaIdentity(
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
        digest="a" * 64,
    )
    payload: dict[str, object] = {
        "schema_identity": identity.to_payload(),
        "provider": "provider-a",
        "model": "model-a",
        "candidate": {"answer": "ok"},
        "attempts": [],
        "token_usage": {"input_tokens": 1},
    }

    with pytest.raises(ValueError):
        StructuredProviderCandidate.model_validate(payload)


@pytest.mark.parametrize(
    "schema_definition",
    [
        {"type": "array", "items": {"type": "string"}},
        {"type": "object", "$ref": "https://example.invalid/output.json"},
        {
            "type": "object",
            "$defs": {"node": {"type": "object", "$ref": "#/$defs/node"}},
            "properties": {"node": {"$ref": "#/$defs/node"}},
        },
        {
            "type": "object",
            "properties": {"node": {"$ref": "#/$defs/missing"}},
        },
        {"type": "object", "additionalProperties": True},
        {
            "type": "object",
            "properties": {"timestamp": {"type": "string", "format": "date-time"}},
        },
        {
            "type": "object",
            "$defs": {"unused": {"type": "string", "format": "email"}},
        },
        {
            "type": "object",
            "$defs": {"unused": {"$ref": "https://example.invalid/output.json"}},
        },
        {
            "type": "object",
            "properties": {"values": {"type": "array", "contains": {"type": "string"}}},
        },
        {"type": "object", "if": {"properties": {"kind": {"const": "a"}}}},
        {"type": "object", "unevaluatedProperties": False},
    ],
)
def test_schema_compiler_rejects_unbounded_or_unsupported_keywords(
    schema_definition: dict[str, object],
) -> None:
    """根数组、remote/recursive/unresolved ref 与关闭关键字都必须编译失败。"""

    with pytest.raises(ValueError):
        compile_output_schema_definition(
            schema_definition,
            schema_ref="fixture.Unsupported",
            version="v1",
        )


def test_schema_compiler_preserves_and_closes_unused_defs_in_canonical_identity() -> None:
    """未引用definitions仍是schema身份的一部分，并须递归收紧object层。"""

    without_defs = compile_output_schema_definition(
        {"type": "object"},
        schema_ref="fixture.Definitions",
        version="v1",
    )
    with_defs = compile_output_schema_definition(
        {
            "type": "object",
            "$defs": {
                "unused": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                }
            },
        },
        schema_ref="fixture.Definitions",
        version="v1",
    )

    assert with_defs.schema_definition["$defs"] == {
        "unused": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        }
    }
    assert with_defs.identity.digest != without_defs.identity.digest


def test_schema_compiler_rejects_non_string_mapping_keys_instead_of_coercing() -> None:
    """业务 schema key 不是 JSON string 时必须关闭失败，不能经 ``str()`` 改写身份。"""

    schema_definition = {
        "type": "object",
        "properties": {1: {"type": "string"}},
    }
    with pytest.raises(ValueError, match="non-string JSON object key"):
        compile_output_schema_definition(  # pyright: ignore[reportArgumentType]
            schema_definition,
            schema_ref="fixture.NonStringKey",
            version="v1",
        )


@pytest.mark.parametrize(
    "candidate",
    [
        ["root-list"],
        _StructuredOutput(answer="sdk-like", nested=_NestedPayload(count=1)),
        {"answer": "nan", "nested": {"count": float("nan")}},
        {"answer": "bytes", "nested": {"count": b"not-json"}},
        {"answer": "tuple", "nested": {"count": (1, 2)}},
    ],
)
def test_candidate_rejects_non_object_or_non_json_provider_values(candidate: object) -> None:
    """SDK/Pydantic、根 list、NaN 与 bytes 不得被隐式字符串化。"""

    identity = OutputSchemaIdentity(
        schema_ref="fixture.Output",
        version="v1",
        digest="a" * 64,
    )
    with pytest.raises(ValueError):
        StructuredProviderCandidate.model_validate(
            {
                "schema_identity": identity,
                "provider": "provider-a",
                "model": "model-a",
                "candidate": candidate,
                "attempts": [
                    ModelAttemptEvidence(
                        attempt=1,
                        side_effect_state="started",
                        outcome="completed",
                        completion_observed=True,
                        input_tokens=1,
                        output_tokens=1,
                        latency_ms=1,
                    )
                ],
            }
        )
