"""可信bound结构化seam的公开red→green合同。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from tests.contracts.model_usage_capacity_test_helpers import event_bus, seed_run

from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelApprovalRequired,
    ModelAttemptEvidence,
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    StructuredProviderCandidate,
    compile_output_schema,
    compile_output_schema_definition,
    structured_provider_prompt,
)
from agent_harness.policy import PolicyEngine, YamlPolicyProvider
from agent_harness.storage import SQLAlchemyStorage, run_migrations


class StructuredOutputFixture(BaseModel):
    """测试 schema 关闭额外字段，避免 fake 自己宣布 structured 成功。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


class _PreparedStructuredCall:
    """公开 provider protocol double；一次 send 对应一个本地 attempt。"""

    def __init__(self, provider: StructuredProviderDouble) -> None:
        self._provider = provider
        self._closed = False

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """首轮返回 invalid，repair 轮返回 valid，用于证明有限控制器归属核心。"""

        self._provider.prompts.append(provider_prompt)
        self._provider.sends.append((repair_ordinal, transport_ordinal))
        candidate = self._provider.candidates[
            min(repair_ordinal, len(self._provider.candidates) - 1)
        ]
        return StructuredProviderCandidate(
            schema_identity=self._provider.schema.identity,
            provider=self._provider.provider_id,
            model="model-a",
            candidate=candidate,
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

    async def aclose(self) -> None:
        """每个 prepared handle 必须且只能由核心清理一次。"""

        assert not self._closed
        self._closed = True
        self._provider.closes += 1


class StructuredProviderDouble:
    """不实现 text complete，证明 structured 不会降级到文本 seam。"""

    provider_id = "provider-a"

    def __init__(
        self,
        schema: OutputSchemaDefinition,
        *,
        candidates: list[dict[str, object]] | None = None,
    ) -> None:
        self.schema = schema
        self.candidates = candidates or [{"wrong": True}, {"answer": "fixed"}]
        self.prompts: list[str] = []
        self.sends: list[tuple[int, int]] = []
        self.closes = 0

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: object,
        schema: OutputSchemaDefinition,
    ) -> _PreparedStructuredCall:
        """只取得本地 handle，不在 prepare 阶段产生 provider request。"""

        assert request.capability == "structured_output"
        assert schema == self.schema
        return _PreparedStructuredCall(self)


class _MutableRouteIdentityRouter(ModelRouter):
    """只改变冻结route identity字段，验证公开replay不会复用旧语义。"""

    allowed_models: tuple[str, ...] = ("model-a",)

    def plan(
        self,
        request: ModelRequest,
        *,
        config: ModelRouterConfig | None = None,
        agent_policy: Any | None = None,
    ) -> ModelRoutePlan:
        """复用真实planning，只让测试显式切换完整identity中的模型集合。"""

        return (
            super()
            .plan(request, config=config, agent_policy=agent_policy)
            .model_copy(update={"allowed_models": self.allowed_models})
        )


class _PromptCapRouter(_MutableRouteIdentityRouter):
    """冻结受控prompt cap，用公开bound seam验证实际send边界的二次断言。"""

    prompt_cap: int

    def structured_prompt_byte_limit(self, plan: ModelRoutePlan) -> int:
        """测试只收窄公开route cap，不改变provider或协调器私有状态。"""

        del plan
        return self.prompt_cap


@pytest.mark.asyncio
async def test_bound_public_seam_rechecks_exact_prompt_cap_before_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planning后prompt若漂移超cap，公开调用必须在provider send前关闭失败。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema, candidates=[{"answer": "unused"}])
    initial_prompt = structured_provider_prompt(
        business_prompt="return an answer",
        schema=schema,
        repair_ordinal=0,
    )
    router = _PromptCapRouter(
        config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
        providers={"provider-a": provider},  # type: ignore[dict-item]
    )
    router.prompt_cap = len(initial_prompt.encode("utf-8"))
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured-prompt-cap.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "prompt-cap-events.jsonl"),
        output_schema_resolver=lambda agent_id: schema,
    )
    prompt_calls = 0

    def drifting_prompt(**kwargs: Any) -> str:
        """Planning返回exact原值，执行期模拟配置外的prompt producer漂移。"""

        nonlocal prompt_calls
        prompt_calls += 1
        prompt = structured_provider_prompt(**kwargs)
        return prompt if prompt_calls == 1 else f"{prompt}x"

    monkeypatch.setattr(
        "agent_harness.models._invocation_structured.structured_provider_prompt",
        drifting_prompt,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                ModelRequest(
                    provider="provider-a",
                    prompt="return an answer",
                    model="model-a",
                    max_output_tokens=8,
                ),
                operation_key="prompt-cap-drift",
            )
        assert failure.value.code == "model.input_too_large"
        assert provider.sends == []
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_public_seam_repairs_once_and_persists_provider_neutral_result(
    tmp_path: Path,
) -> None:
    """公开 seam 自动使用当前 Agent schema，并保留 canonical output_text。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema)
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'structured.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    router = _MutableRouteIdentityRouter(
        config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
        providers={"provider-a": provider},  # type: ignore[dict-item]
    )
    service = ModelInvocationService(
        router=router,
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "events.jsonl"),
        output_schema_resolver=lambda agent_id: schema,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        response = await bound.complete_structured(
            ModelRequest(
                provider="provider-a",
                prompt="return an answer",
                model="model-a",
                max_output_tokens=8,
            ),
            operation_key="primary-structured",
            repair_limit=1,
        )

        assert response.output_text == '{"answer":"fixed"}'
        assert response.structured_output is not None
        assert response.structured_output.value == {"answer": "fixed"}
        assert response.structured_output.repair_count == 1
        assert response.structured_output.provider_request_count == 2
        assert len(response.structured_output.replay_identity) == 64
        assert provider.sends == [(0, 1), (1, 1)]
        assert provider.closes == 2
        assert all("wrong" not in prompt for prompt in provider.prompts[1:])
        durable_payload = response.to_payload()
        assert "structured_result" not in durable_payload
        assert set(durable_payload["attempts"][0]) == {
            "attempt",
            "side_effect_state",
            "outcome",
            "completion_observed",
            "http_status",
            "retry_after_ms",
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "cost_status",
            "budget_charge_tokens",
            "budget_charge_cost_usd",
            "latency_ms",
            "error_code",
            "structured_output",
        }
        assert set(durable_payload["attempts"][0]["structured_output"]) == {
            "schema_version",
            "schema_identity",
            "phase",
            "repair_ordinal",
            "transport_ordinal",
            "prompt_digest",
            "repair_trigger_codes",
            "validation_codes",
            "not_started_proof",
            "cleanup_status",
        }
        assert durable_payload["attempts"][0]["structured_output"]["validation_codes"] == [
            "extra_field",
            "missing_required",
        ]
        assert durable_payload["attempts"][1]["structured_output"]["repair_trigger_codes"] == [
            "extra_field",
            "missing_required",
        ]
        assert durable_payload["attempts"][1]["structured_output"]["validation_codes"] == []

        replayed = await bound.complete_structured(
            ModelRequest(
                provider="provider-a",
                prompt="return an answer",
                model="model-a",
                max_output_tokens=8,
            ),
            operation_key="primary-structured",
            repair_limit=1,
        )
        assert replayed == response
        assert provider.sends == [(0, 1), (1, 1)]

        with pytest.raises(ModelProviderInvocationError) as conflict:
            await bound.complete_structured(
                ModelRequest(
                    provider="provider-a",
                    prompt="different semantic prompt",
                    model="model-a",
                    max_output_tokens=8,
                ),
                operation_key="primary-structured",
                repair_limit=1,
            )
        assert conflict.value.code == "model.structured_replay_conflict"
        assert provider.sends == [(0, 1), (1, 1)]

        router.allowed_models = ("model-a", "model-b")
        with pytest.raises(ModelProviderInvocationError) as route_conflict:
            await bound.complete_structured(
                ModelRequest(
                    provider="provider-a",
                    prompt="return an answer",
                    model="model-a",
                    max_output_tokens=8,
                ),
                operation_key="primary-structured",
                repair_limit=1,
            )
        assert route_conflict.value.code == "model.structured_replay_conflict"
        assert provider.sends == [(0, 1), (1, 1)]
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_bound_public_seam_rejects_extra_field_in_nullable_object(
    tmp_path: Path,
) -> None:
    """nullable object同样必须递归关闭，不能让provider double绕过核心校验。"""

    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {"payload": {"type": ["object", "null"]}},
            "required": ["payload"],
        },
        schema_ref="agents.example.schemas.NullablePayload",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(
        schema,
        candidates=[{"payload": {"unexpected": 1}}],
    )
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'nullable-extra.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
            providers={"provider-a": provider},  # type: ignore[dict-item]
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "events.jsonl"),
        output_schema_resolver=lambda agent_id: schema,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )

        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                ModelRequest(
                    provider="provider-a",
                    prompt="return nullable payload",
                    model="model-a",
                    max_output_tokens=8,
                ),
                operation_key="nullable-extra",
                repair_limit=0,
            )

        assert failure.value.code == "model.structured_extra_fields"
        assert failure.value.provider_called is True
        assert failure.value.attempt_count == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_provider", "expected_code"),
    [
        (YamlPolicyProvider(deny_actions=["model.invoke"]), "model.policy_denied"),
        (
            YamlPolicyProvider(require_approval_actions=["model.invoke"]),
            "model.approval_required",
        ),
    ],
)
async def test_bound_structured_seam_applies_model_policy_before_provider_and_claim(
    tmp_path: Path,
    policy_provider: YamlPolicyProvider,
    expected_code: str,
) -> None:
    """DENY 与 REQUIRE_APPROVAL 都必须在 claim、client、send 前关闭。"""

    schema = compile_output_schema(
        StructuredOutputFixture,
        schema_ref="agents.example.schemas.Output",
        version="1.0.0",
    )
    provider = StructuredProviderDouble(schema, candidates=[{"answer": "must-not-send"}])
    dsn = f"sqlite+aiosqlite:///{tmp_path / f'policy-{expected_code}.sqlite3'}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    service = ModelInvocationService(
        router=ModelRouter(
            config=ModelRouterConfig(default_provider="provider-a", default_model="model-a"),
            providers={"provider-a": provider},  # type: ignore[dict-item]
        ),
        storage=storage,
        event_bus=event_bus(storage=storage, event_path=tmp_path / "policy-events.jsonl"),
        policy_engine=PolicyEngine(provider=policy_provider),
        output_schema_resolver=lambda agent_id: schema,
    )
    try:
        run_id = await seed_run(storage, request_id="request-a")
        bound = service.bind_execution(
            identity=IdentityContext(
                tenant_id="tenant-a",
                user_id="user-a",
                session_id="session-a",
            ),
            tenant_id="tenant-a",
            run_id=run_id,
            agent_id="agent-a",
            request_id="request-a",
            trace_id="trace-a",
        )
        request = ModelRequest(
            provider="provider-a",
            prompt='{"tool":"must-not-execute"}',
            model="model-a",
            estimated_input_tokens=3,
            max_output_tokens=8,
        )

        if expected_code == "model.approval_required":
            with pytest.raises(ModelApprovalRequired) as captured:
                await bound.complete_structured(
                    request,
                    operation_key="policy-structured",
                    repair_limit=1,
                )
            approval = captured.value.request
            assert approval.continuation["kind"] == "structured_policy_approval"
            assert set(approval.continuation) == {
                "schema_version",
                "kind",
                "usage_call_id",
                "operation_identity_digest",
                "schema_identity",
                "repair_limit",
                "arguments_hash",
            }
            assert approval.arguments_hash == approval.continuation["arguments_hash"]
        else:
            with pytest.raises(ModelProviderInvocationError) as captured:
                await bound.complete_structured(
                    request,
                    operation_key="policy-structured",
                    repair_limit=1,
                )
            assert captured.value.code == expected_code

        assert provider.sends == []
        async with storage.uow() as uow:
            with pytest.raises(LookupError):
                await uow.evidence_outbox.get_usage(
                    tenant_id="tenant-a",
                    usage_call_id="not-a-real-call-id",
                )
    finally:
        await service.aclose()
        await storage.dispose()
