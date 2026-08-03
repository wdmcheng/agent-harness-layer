"""直接构造descriptor的严格结构化输出身份夹具。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from agent_harness.models import (
    ModelAttemptEvidence,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
    ModelRoutePlan,
    OutputSchemaDefinition,
    OutputSchemaIdentity,
    PreparedStructuredModelCall,
    StructuredProviderCandidate,
    compile_output_schema_definition,
)

_FIXTURE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}


class StructuredCrashAfterSend(BaseException):
    """模拟 durable mark 已提交、structured send 边界后进程被强制终止。"""


class _CrashPreparedCall:
    """只在 send 处崩溃的单次 prepared handle，用于预算恢复合同。"""

    def __init__(self, provider: StructuredCrashProvider) -> None:
        self._provider = provider

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """记录唯一 request 后模拟进程终止，不返回候选或用量。"""

        assert provider_prompt
        self._provider.sends.append((repair_ordinal, transport_ordinal))
        raise StructuredCrashAfterSend

    async def aclose(self) -> None:
        """证明异常退出仍由核心恰好清理一次 prepared handle。"""

        self._provider.closes += 1


class StructuredCrashProvider:
    """同时满足 text 基协议与 structured 协议的 send-boundary 崩溃 double。"""

    provider_id = "fake"

    def __init__(self, schema: OutputSchemaDefinition) -> None:
        """冻结期望 schema，并初始化可断言的 send/close 事实。"""

        self.schema = schema
        self.sends: list[tuple[int, int]] = []
        self.closes = 0

    async def complete(self, request: ModelRequest, *, plan: ModelRoutePlan) -> ModelResponse:
        """该 double 不允许 text fallback；误调用立即让合同失败。"""

        raise AssertionError((request, plan))

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> _CrashPreparedCall:
        """只取得本地 handle，不记录 request；send 才越过副作用边界。"""

        assert request.capability == "structured_output"
        assert plan.provider == self.provider_id
        assert schema == self.schema
        return _CrashPreparedCall(self)


class _MalformedStructuredHandle:
    """在send或cleanup属性边界注入运行期Protocol违规。"""

    def __init__(self, owner: MalformedStructuredHandleProvider) -> None:
        self._owner = owner

    def __getattribute__(self, name: str) -> object:
        """只篡改指定公开属性，其余handle行为保持可验证。"""

        if name in {"send_structured", "aclose"}:
            owner = object.__getattribute__(self, "_owner")
            mutation = owner.mutation
            if mutation == f"missing_{name}":
                raise AttributeError(name)
            if mutation == f"raising_{name}":
                raise RuntimeError(f"malformed {name} property")
            if mutation == f"noncallable_{name}":
                return object()
            if mutation == "sync_send_structured" and name == "send_structured":

                def sync_send(**_: object) -> None:
                    """记录已越过调用边界，但故意不返回awaitable。"""

                    owner.sync_send_calls += 1

                return sync_send
            if mutation == "sync_aclose" and name == "aclose":
                return lambda: None
        return object.__getattribute__(self, name)

    async def send_structured(
        self,
        *,
        provider_prompt: str,
        repair_ordinal: int,
        transport_ordinal: int,
    ) -> StructuredProviderCandidate:
        """正常分支返回有效候选；畸形send属性不得进入本函数体。"""

        assert provider_prompt
        self._owner.sends.append((repair_ordinal, transport_ordinal))
        attempt = ModelAttemptEvidence(
            attempt=1,
            side_effect_state="started",
            outcome="completed",
            completion_observed=True,
            input_tokens=1,
            output_tokens=1,
            cost_status="unavailable",
            latency_ms=1,
        )
        return StructuredProviderCandidate(
            schema_identity=self._owner.schema.identity,
            provider=self._owner.provider_id,
            model=self._owner.model,
            candidate={"answer": "handle-boundary"},
            attempts=[attempt],
        )

    async def aclose(self) -> None:
        """正常cleanup只记录一次本地资源释放。"""

        self._owner.closes += 1


class MalformedStructuredHandleProvider:
    """从公开provider seam暴露畸形send/cleanup属性，不执行网络。"""

    provider_id = "provider-a"
    model = "model-a"

    def __init__(self, schema: OutputSchemaDefinition, *, mutation: str) -> None:
        self.schema = schema
        self.mutation = mutation
        self.prepares = 0
        self.sends: list[tuple[int, int]] = []
        self.sync_send_calls = 0
        self.closes = 0

    async def prepare_structured(
        self,
        request: ModelRequest,
        *,
        plan: ModelRoutePlan,
        schema: OutputSchemaDefinition,
    ) -> PreparedStructuredModelCall:
        """返回运行期协议违规对象，证明核心不依赖静态类型获得安全性。"""

        assert request.capability == "structured_output"
        assert plan.provider == self.provider_id
        assert schema == self.schema
        self.prepares += 1
        return cast(PreparedStructuredModelCall, _MalformedStructuredHandle(self))


async def assert_cancellation_resistant_cleanup_is_bounded(tmp_path: Path) -> None:
    """从公开bound seam验证吞取消cleanup有界返回、显式持有并最终回收。"""

    from tests.contracts.test_provider_neutral_structured_transport_contracts import (
        ControlledStructuredProvider,
        ShortStructuredDeadlineRouter,
        build_structured_bound,
        structured_request,
        structured_schema,
    )

    schema = structured_schema()
    close_gate = asyncio.Event()
    close_started = asyncio.Event()
    close_finished = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        close_gate=close_gate,
        close_started=close_started,
        close_finished=close_finished,
        close_resists_cancel=True,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
        router_type=ShortStructuredDeadlineRouter,
    )
    task = asyncio.create_task(
        bound.complete_structured(
            structured_request(), operation_key="cancellation-resistant-close"
        )
    )
    try:
        await close_started.wait()
        await asyncio.sleep(0.25)
        assert task.done(), "cancellation-resistant cleanup escaped total deadline"
        try:
            await task
        except ModelProviderInvocationError as failure:
            assert failure.code == "model.provider_side_effect_unknown"
        else:
            raise AssertionError("cancellation-resistant cleanup must fail closed")
        assert provider.close_cancellations == 1
        assert not close_finished.is_set()
        pending_cleanup = vars(service).get("_structured_cleanup_tasks")
        assert isinstance(pending_cleanup, set)
        pending_cleanup = cast(set[object], pending_cleanup)
        assert len(pending_cleanup) == 1
        close_gate.set()
        await asyncio.wait_for(close_finished.wait(), timeout=0.1)
        await asyncio.sleep(0)
        assert not pending_cleanup
    finally:
        close_gate.set()
        if not task.done():
            try:
                await task
            except BaseException:
                pass
        await service.aclose()
        await storage.dispose()


def fixture_output_schema_identity(
    *,
    schema_ref: str = "fixture.Output",
    version: str = "v1",
) -> OutputSchemaIdentity:
    """从真实严格 schema 编译 identity，不用默认值或伪造 digest 绕过必填项。"""

    return compile_output_schema_definition(
        _FIXTURE_OUTPUT_SCHEMA,
        schema_ref=schema_ref,
        version=version,
    ).identity
