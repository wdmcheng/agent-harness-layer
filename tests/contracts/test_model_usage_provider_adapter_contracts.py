"""模型供应商适配器的用量归一化、脱敏与失败封闭合同测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.models import (
    ModelInvocationService,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
    UsageEvidenceContext,
)
from agent_harness.storage import RunCreate, SessionCreate, SQLAlchemyStorage, run_migrations


async def _usage_run(storage: SQLAlchemyStorage) -> str:
    """在独立租户、会话和运行中创建可持久化用量证据的最小上下文。"""

    async with storage.uow() as uow:
        await uow.tenants.ensure("tenant-a")
        await uow.sessions.ensure(
            SessionCreate(
                session_id="session-a",
                tenant_id="tenant-a",
                user_id="user-a",
                agent_id="agent-a",
            )
        )
        run = await uow.runs.create(
            RunCreate(
                tenant_id="tenant-a",
                session_id="session-a",
                agent_id="agent-a",
                trace_id="trace-a",
            )
        )
        await uow.commit()
        return run.id


@pytest.mark.asyncio
async def test_pydantic_ai_timeout_is_closed_as_provider_failure_evidence(
    tmp_path: Path,
) -> None:
    """供应商超时必须归一为公开失败码，同时不把密钥或提示词写入事件。"""

    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class UnusedAgent:
        """若超时 seam 未优先执行就失败，防止测试走到真实供应商调用。"""

        async def run(self, prompt: str, *, model_settings: object) -> Any:
            """制造含敏感片段的 async timeout，验证异常不会进入 evidence。"""

            del prompt, model_settings
            raise TimeoutError("Authorization=Bearer timeout-secret; raw prompt")

    def agent_factory(_: object) -> UnusedAgent:
        """返回永不应被使用的代理替身。"""

        return UnusedAgent()

    database = tmp_path / "timeout.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "timeout-events.jsonl")

    async def resolve_trace(**_: object) -> str:
        """为事件总线提供稳定追踪标识，避免测试依赖外部上下文。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "openai-compatible": PydanticAIModelProvider(
                        provider_id="openai-compatible",
                        agent_factory=cast(Any, agent_factory),
                    )
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )

        with pytest.raises(ModelProviderInvocationError) as exc_info:
            await service.complete(
                ModelRequest(
                    provider="openai-compatible",
                    prompt="private timeout prompt",
                    max_output_tokens=1,
                    timeout_seconds=1,
                ),
                context=UsageEvidenceContext(
                    tenant_id="tenant-a",
                    run_id=run_id,
                    agent_id="agent-a",
                    trace_id="trace-a",
                ),
                usage_call_id="usage-timeout",
            )

        # 除公开错误码外，持久化事件中不能留下供应商异常或用户输入的原文。
        events = await sink.read(run_id=run_id)
        assert exc_info.value.code == "model.provider_side_effect_unknown"
        assert events[-1].payload is not None
        assert events[-1].payload["outcome"] == "failed"
        assert events[-1].payload["error_code"] == "model.provider_side_effect_unknown"
        assert events[-1].terminal is False
        serialized = (tmp_path / "timeout-events.jsonl").read_text(encoding="utf-8")
        assert "timeout-secret" not in serialized
        assert "private timeout prompt" not in serialized
        assert "raw prompt" not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_ai_success_is_normalized_before_usage_persistence(tmp_path: Path) -> None:
    """成功响应需在写入事件前抽取统一 token 字段，并隔离响应正文与提示词。"""

    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class Result:
        """模拟供应商响应：业务正文与用量对象通过不同属性暴露。"""

        output = "provider raw output private"

        class Usage:
            """模拟供应商提供的输入、输出 token 计数。"""

            input_tokens = 7
            output_tokens = 4

        def usage(self) -> Usage:
            """返回供应商原生用量对象，供适配器归一化。"""

            return self.Usage()

    class Agent:
        """只接受预期私密提示词的异步供应商代理替身。"""

        async def run(self, prompt: str, *, model_settings: object) -> Result:
            """验证提示词抵达适配器后返回预设响应。"""

            assert prompt == "private provider prompt"
            assert isinstance(model_settings, dict)
            assert model_settings["max_tokens"] == 5
            return Result()

    def agent_factory(_: object) -> Agent:
        """构造无网络依赖的供应商代理。"""

        return Agent()

    database = tmp_path / "pydantic-success.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "pydantic-success.jsonl")

    async def resolve_trace(**_: object) -> str:
        """固定事件追踪标识，以聚焦适配器输出而非上下文解析。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "openai-compatible": PydanticAIModelProvider(
                        provider_id="openai-compatible",
                        agent_factory=cast(Any, agent_factory),
                    )
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        response = await service.complete(
            ModelRequest(
                provider="openai-compatible",
                prompt="private provider prompt",
                max_output_tokens=5,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-pydantic-success",
        )

        # 调用结果可返回给业务层，但审计事件只能保留归一化后的计量信息。
        events = await sink.read(run_id=run_id)
        assert response.output_text == "provider raw output private"
        assert events[-1].payload is not None
        usage = cast(dict[str, Any], events[-1].payload["usage"])
        assert usage["provider"] == "openai-compatible"
        assert usage["model"] == "openai:test"
        assert usage["input_tokens"] == 7
        assert usage["output_tokens"] == 4
        serialized = (tmp_path / "pydantic-success.jsonl").read_text(encoding="utf-8")
        assert "private provider prompt" not in serialized
        assert "provider raw output private" not in serialized
    finally:
        await storage.dispose()


@pytest.mark.asyncio
async def test_pydantic_ai_missing_usage_preserves_unknown_tokens_as_null(tmp_path: Path) -> None:
    """供应商未返回用量时保留未知值，不能用估算值伪装成已确认计量。"""

    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    class Result:
        """不提供 usage seam 的供应商成功响应。"""

        output = "two words"

    class Agent:
        """返回无用量字段的响应，覆盖适配器的缺失信息分支。"""

        async def run(self, prompt: str, *, model_settings: object) -> Result:
            """验证请求照常送达供应商，同时不伪造计量对象。"""

            assert prompt == "private provider prompt"
            assert isinstance(model_settings, dict)
            assert model_settings["max_tokens"] == 5
            return Result()

    def agent_factory(_: object) -> Agent:
        """构造本地代理替身，避免该合同测试产生外部调用。"""

        return Agent()

    database = tmp_path / "pydantic-missing-usage.db"
    dsn = f"sqlite+aiosqlite:///{database}"
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "pydantic-missing-usage.jsonl")

    async def resolve_trace(**_: object) -> str:
        """返回稳定 trace，保证事件可按当前运行读取。"""

        return "trace-a"

    try:
        run_id = await _usage_run(storage)
        service = ModelInvocationService(
            router=ModelRouter(
                config=ModelRouterConfig(default_model="openai:test"),
                providers={
                    "openai-compatible": PydanticAIModelProvider(
                        provider_id="openai-compatible",
                        agent_factory=cast(Any, agent_factory),
                    )
                },
            ),
            storage=storage,
            event_bus=EventBus(sink=sink, run_trace_resolver=resolve_trace),
        )
        response = await service.complete(
            ModelRequest(
                provider="openai-compatible",
                prompt="private provider prompt",
                estimated_input_tokens=7,
                max_output_tokens=5,
            ),
            context=UsageEvidenceContext(
                tenant_id="tenant-a",
                run_id=run_id,
                agent_id="agent-a",
                trace_id="trace-a",
            ),
            usage_call_id="usage-pydantic-missing",
        )

        # 业务侧 token 字典为空，审计载荷则明确以 null 表示供应商未知而非零消耗。
        events = await sink.read(run_id=run_id)
        assert response.token_usage == {}
        assert events[-1].payload is not None
        assert events[-1].payload["usage"]["input_tokens"] is None
        assert events[-1].payload["usage"]["output_tokens"] is None
    finally:
        await storage.dispose()
