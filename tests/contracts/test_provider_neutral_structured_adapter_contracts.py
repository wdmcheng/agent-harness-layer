"""Pydantic AI adapter的provider-neutral结构化边界合同。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

import pytest
from pydantic import BaseModel, ConfigDict
from tests.contracts.controlled_real_model_runtime_composition_test_support import (
    controlled_route,
)
from tests.contracts.provider_neutral_structured_output_test_support import (
    MalformedStructuredHandleProvider,
)
from tests.contracts.test_provider_neutral_structured_transport_contracts import (
    build_structured_bound,
    structured_request,
    structured_schema,
)

from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
from agent_harness.models import (
    ModelAttemptEvidence,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRoutePlan,
    ModelRouter,
    ModelRouterConfig,
    OutputSchemaDefinition,
    StructuredProviderCallError,
    StructuredProviderCandidate,
    StructuredProviderPrepareError,
    compile_output_schema,
)


def test_structured_provider_failure_dto_rejects_unknown_outcome() -> None:
    """确定失败错误码不得与unknown attempt组成可持久化联合体。"""

    with pytest.raises(ValueError, match="definite failed attempt"):
        StructuredProviderCallError(
            code="model.provider_failed",
            attempts=[
                ModelAttemptEvidence(
                    attempt=1,
                    side_effect_state="started",
                    outcome="unknown",
                    completion_observed=True,
                    input_tokens=1,
                    output_tokens=1,
                    cost_status="unavailable",
                    latency_ms=1,
                    error_code="model.provider_failed",
                )
            ],
        )


class _ContradictoryFailurePrepared:
    """模拟provider把未知结果错标为确定失败的协议违规窗口。"""

    def __init__(self, owner: _ContradictoryFailureProvider, *, mutation: str) -> None:
        self._owner = owner
        self._mutation = mutation

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """分别覆盖构造期拒绝与构造后篡改，确保核心仍fail closed。"""

        assert provider_prompt
        self._owner.sends.append((repair_ordinal, transport_ordinal))
        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state="started",
            outcome="unknown" if self._mutation == "constructor_unknown" else "failed",
            completion_observed=True,
            input_tokens=1,
            output_tokens=1,
            cost_status="unavailable",
            latency_ms=1,
            error_code="model.provider_failed",
        )
        if self._mutation == "duck_candidate":
            completed = attempt.model_copy(update={"outcome": "completed", "error_code": None})
            return cast(
                StructuredProviderCandidate,
                _DuckCandidate(
                    StructuredProviderCandidate(
                        schema_identity=structured_schema().identity,
                        provider="provider-a",
                        model="model-a",
                        candidate={"answer": "duck-bypass"},
                        attempts=[completed],
                    ).model_dump(mode="python")
                ),
            )
        error_type = (
            _ExplodingValidationCallError
            if self._mutation == "override_validator"
            else StructuredProviderCallError
        )
        error = error_type(
            code="model.provider_failed",
            attempts=[attempt],
        )
        if self._mutation == "replace_outcome":
            error.attempts = (attempt.model_copy(update={"outcome": "unknown"}),)
        elif self._mutation == "clear_attempts":
            error.attempts = ()
        elif self._mutation == "append_attempt":
            error.attempts = (attempt, attempt)
        elif self._mutation == "delete_attempts":
            del error.attempts
        elif self._mutation == "delete_code":
            del error.code
        elif self._mutation == "replace_code":
            error.code = "model.invalid_provider_error"
        elif self._mutation == "delete_attempt_outcome":
            del error.attempts[0].outcome
        raise error

    async def aclose(self) -> None:
        """记录唯一cleanup，证明exact replay没有再次取得provider资源。"""

        self._owner.closes += 1


class _ContradictoryFailureProvider:
    """从公开provider seam注入矛盾attempt，不执行网络。"""

    provider_id = "provider-a"

    def __init__(self, *, mutation: str) -> None:
        self.mutation = mutation
        self.prepares = 0
        self.sends: list[tuple[int, int]] = []
        self.closes = 0

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> _ContradictoryFailurePrepared:
        """返回单次prepared handle；identity只用于证明走正式公开路由。"""

        assert request.capability == "structured_output"
        assert plan.provider == self.provider_id
        assert schema == structured_schema()
        self.prepares += 1
        return _ContradictoryFailurePrepared(
            self,
            mutation=self.mutation,
        )


class _ExplodingValidationCallError(StructuredProviderCallError):
    """证明核心不得动态派发adapter可覆盖的错误验证方法。"""

    def validated_attempt(self) -> Never:
        """若执行器错误地信任实例方法，则稳定暴露该协议违规。"""

        raise RuntimeError("adapter validator override must not execute")


class _DuckCandidate:
    """只伪造序列化方法的非核心对象，不能进入candidate公共边界。"""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def model_dump(self, *, mode: str) -> dict[str, object]:
        """返回表面合法的payload，证明核心仍须验证对象精确类型。"""

        assert mode == "python"
        return self._payload


@pytest.mark.parametrize(
    "mutation",
    [
        "constructor_unknown",
        "replace_outcome",
        "clear_attempts",
        "append_attempt",
        "delete_attempts",
        "delete_code",
        "replace_code",
        "delete_attempt_outcome",
        "override_validator",
        "duck_candidate",
    ],
)
@pytest.mark.asyncio
async def test_unknown_provider_outcome_is_needs_review_and_exact_replay_is_fenced(
    tmp_path: Path,
    mutation: str,
) -> None:
    """构造期或后置篡改的unknown结果都不得发布failed或在重放时重调。"""

    schema = structured_schema()
    provider = _ContradictoryFailureProvider(mutation=mutation)
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(),
                    operation_key=f"contradictory-provider-{mutation}",
                )
            assert failure.value.code == "model.provider_side_effect_unknown"
            assert failure.value.provider_called is True
            assert failure.value.attempt_count == 1
        assert provider.prepares == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1

        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            assert rows[0].state == "published"
            assert rows[0].error_code == "model.provider_side_effect_unknown"
            assert rows[0].result_json is not None
            result = deepcopy(rows[0].result_json)
        summary = result["evidence"]["decision"]["structured_output"]
        assert summary["status"] == "needs_review"
        assert summary["provider_request_count"] == 1
        assert result["evidence"]["decision"]["attempts"][0]["outcome"] == "unknown"
    finally:
        await service.aclose()
        await storage.dispose()


class _PrepareErrorSubclass(StructuredProviderPrepareError):
    """模拟adapter以子类扩大核心prepare错误语义。"""


class _MalformedPrepareProvider:
    """从公开prepare seam注入缺失、错型或子类retryable证据。"""

    provider_id = "provider-a"

    def __init__(self, *, mutation: str) -> None:
        self.mutation = mutation
        self.prepares = 0

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> Never:
        """prepare失败前不产生send；畸形错误只能按非retryable失败关闭。"""

        assert request.capability == "structured_output"
        assert plan.provider == self.provider_id
        assert schema == structured_schema()
        self.prepares += 1
        error_type = (
            _PrepareErrorSubclass if self.mutation == "subclass" else StructuredProviderPrepareError
        )
        error = error_type(retryable=True)
        if self.mutation == "delete_retryable":
            del error.retryable
        elif self.mutation == "replace_retryable":
            object.__setattr__(error, "retryable", "yes")
        raise error


@pytest.mark.parametrize(
    "mutation",
    ["delete_retryable", "replace_retryable", "subclass"],
)
@pytest.mark.asyncio
async def test_malformed_prepare_error_is_nonretryable_and_exact_replay_is_fenced(
    tmp_path: Path,
    mutation: str,
) -> None:
    """不可信prepare错误不得裸漏、重试或在exact replay时再次触达provider。"""

    provider = _MalformedPrepareProvider(mutation=mutation)
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=structured_schema(),
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(),
                    operation_key=f"malformed-prepare-{mutation}",
                )
            assert failure.value.code == "model.provider_failed"
            assert failure.value.provider_called is False
            assert failure.value.attempt_count == 1
        assert provider.prepares == 1
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            terminal = (rows[0].state, rows[0].error_code)
        assert terminal == ("published", "model.provider_failed")
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.parametrize(
    "mutation",
    ["missing_aclose", "raising_aclose", "sync_aclose"],
)
@pytest.mark.asyncio
async def test_malformed_cleanup_handle_is_needs_review_and_exact_replay_is_fenced(
    tmp_path: Path,
    mutation: str,
) -> None:
    """send 后无法证明 cleanup 完成时必须 needs-review，且 exact replay 不得重调。"""

    schema = structured_schema()
    provider = MalformedStructuredHandleProvider(schema, mutation=mutation)
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(),
                    operation_key=f"malformed-cleanup-{mutation}",
                )
            assert failure.value.code == "model.provider_side_effect_unknown"
            assert failure.value.provider_called is True
            assert failure.value.attempt_count == 1
        assert provider.prepares == 1
        assert provider.sends == [(0, 1)]
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            assert rows[0].result_json is not None
            terminal = (rows[0].state, rows[0].error_code)
            result = deepcopy(rows[0].result_json)
        assert terminal == ("published", "model.provider_side_effect_unknown")
        summary = result["evidence"]["decision"]["structured_output"]
        assert summary["status"] == "needs_review"
        assert summary["provider_request_count"] == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.parametrize(
    "mutation",
    ["missing_send_structured", "raising_send_structured", "noncallable_send_structured"],
)
@pytest.mark.asyncio
async def test_malformed_send_attribute_preserves_exact_zero_request_replay(
    tmp_path: Path,
    mutation: str,
) -> None:
    """send属性尚未成功解析或调用时，耐久证据必须保留精确零请求。"""

    schema = structured_schema()
    provider = MalformedStructuredHandleProvider(schema, mutation=mutation)
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(),
                    operation_key=f"malformed-send-{mutation}",
                )
            assert failure.value.code == "model.provider_failed"
            assert failure.value.provider_called is False
            assert failure.value.attempt_count == 1
        assert (provider.prepares, provider.sends, provider.closes) == (1, [], 1)
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            assert rows[0].result_json is not None
            result = deepcopy(rows[0].result_json)
        evidence = result["evidence"]["decision"]
        assert evidence["provider_called"] is False
        assert evidence["structured_output"]["provider_request_count"] == 0
        assert evidence["attempts"][0]["side_effect_state"] == "not_started"
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_called_send_with_nonawaitable_result_keeps_one_request_fenced(
    tmp_path: Path,
) -> None:
    """callable一旦执行就必须保守计一次请求，即使返回值违反awaitable协议。"""

    schema = structured_schema()
    provider = MalformedStructuredHandleProvider(schema, mutation="sync_send_structured")
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        for _ in range(2):
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(), operation_key="sync-send-nonawaitable"
                )
            assert failure.value.code == "model.provider_side_effect_unknown"
            assert failure.value.provider_called is True
            assert failure.value.attempt_count == 1
        assert (provider.prepares, provider.sync_send_calls, provider.closes) == (1, 1, 1)
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            assert rows[0].result_json is not None
            result = deepcopy(rows[0].result_json)
        evidence = result["evidence"]["decision"]
        assert evidence["provider_called"] is True
        assert evidence["structured_output"]["provider_request_count"] == 1
    finally:
        await service.aclose()
        await storage.dispose()


class _Output(BaseModel):
    """Adapter 只回传候选；该 schema 的业务校验仍归核心。"""

    model_config = ConfigDict(extra="forbid")

    answer: str


@dataclass
class _Usage:
    """模拟 SDK usage 的两个明确 token 维度。"""

    input_tokens: int = 3
    output_tokens: int = 2


class _Result:
    """模拟 SDK run result，输出类型由 case 显式控制。"""

    def __init__(self, output: object) -> None:
        self.output = output

    @property
    def usage(self) -> _Usage:
        """返回完整 usage 属性，确保 sole attempt 可结算。"""

        return _Usage()


class _Agent:
    """记录 run 调用，证明 structured 每次强制 retries=0。"""

    def __init__(self, output: object) -> None:
        self.output = output
        self.calls: list[tuple[str, int | None, int | None]] = []

    async def run(
        self,
        prompt: str,
        *,
        model_settings: object,
        retries: int | None = None,
    ) -> _Result:
        """单次返回固定候选，不执行工具、不做内部 repair。"""

        settings = cast(dict[str, object], model_settings)
        max_tokens = settings.get("max_tokens")
        assert max_tokens is None or isinstance(max_tokens, int)
        self.calls.append((prompt, max_tokens, retries))
        return _Result(self.output)


def _route(agent: _Agent) -> tuple[PydanticAIModelProvider, ModelRequest, ModelRoutePlan]:
    """从现有typed deployment取得route，再仅切换结构化输出capability。"""

    settings, request, policy, _model_settings = controlled_route()
    provider = PydanticAIModelProvider(agent_factory=lambda _plan: agent)
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    text_plan = router.plan(request, agent_policy=policy)
    plan = text_plan.model_copy(
        update={
            "capability": "structured_output",
            "max_attempts": 1,
            "total_timeout_ms": 1_000,
        }
    )
    return provider, request.model_copy(update={"capability": "structured_output"}), plan


@pytest.mark.asyncio
async def test_adapter_returns_one_candidate_and_disables_sdk_retries() -> None:
    """一个 Harness transport ordinal 精确对应一个 Agent.run(retries=0)。"""

    agent = _Agent({"answer": "ok"})
    provider, request, plan = _route(agent)
    schema = compile_output_schema(
        _Output,
        schema_ref="fixture.Output",
        version="v1",
    )
    prepared = await provider.prepare_structured(request, plan=plan, schema=schema)
    try:
        candidate = await prepared.send_structured(
            provider_prompt="structured prompt",
            repair_ordinal=0,
            transport_ordinal=1,
        )
    finally:
        await prepared.aclose()
        await provider.aclose()

    assert agent.calls == [("structured prompt", 17, 0)]
    assert candidate.candidate == {"answer": "ok"}
    assert candidate.schema_identity == schema.identity
    assert len(candidate.attempts) == 1
    assert candidate.attempts[0].attempt == 1
    assert candidate.attempts[0].input_tokens == 3
    assert candidate.attempts[0].output_tokens == 2


@pytest.mark.asyncio
async def test_adapter_rejects_sdk_or_pydantic_output_without_stringifying_it() -> None:
    """任意 Python/Pydantic 对象不得通过 str() 进入核心 candidate。"""

    agent = _Agent(_Output(answer="must-not-stringify"))
    provider, request, plan = _route(agent)
    schema = compile_output_schema(
        _Output,
        schema_ref="fixture.Output",
        version="v1",
    )
    prepared = await provider.prepare_structured(request, plan=plan, schema=schema)
    try:
        with pytest.raises(StructuredProviderCallError) as failure:
            await prepared.send_structured(
                provider_prompt="structured prompt",
                repair_ordinal=0,
                transport_ordinal=1,
            )
        assert failure.value.code == "model.provider_failed"
        assert len(failure.value.attempts) == 1
        assert failure.value.attempts[0].side_effect_state == "started"
    finally:
        await prepared.aclose()
        await provider.aclose()


@pytest.mark.asyncio
async def test_existing_text_complete_keeps_output_text_and_default_sdk_retry_argument() -> None:
    """Structured adapter 增量不得改变既有 text-only complete 行为。"""

    agent = _Agent("text-result")
    provider, structured_request, plan = _route(agent)
    text_request = structured_request.model_copy(update={"capability": "text_completion"})
    text_plan = plan.model_copy(update={"capability": "text_completion"})
    try:
        response = await provider.complete(text_request, plan=text_plan)
    finally:
        await provider.aclose()

    assert response.output_text == "text-result"
    assert response.structured_output is None
    assert agent.calls == [(text_request.prompt, 17, None)]
