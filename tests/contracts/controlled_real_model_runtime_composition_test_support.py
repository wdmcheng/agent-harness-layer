"""真实 adapter、lazy client 与默认 fake 离线组合合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import HarnessSettings, ModelSettings, load_settings
from agent_harness.models import (
    BoundModelInvocationService,
    ModelRequest,
)
from agent_harness.registry import (
    AgentModelPolicy,
)
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
)


@dataclass
class UsageDouble:
    """模拟 Pydantic AI result usage 的最小 provider-neutral 读面。"""

    input_tokens: int = 3
    output_tokens: int = 2


class ResultDouble:
    """返回固定文本与完整 usage，避免真实 provider 或网络。"""

    output: str = "adapter-result"

    def usage(self) -> UsageDouble:
        return UsageDouble()


class AsyncAgentDouble:
    """记录 Agent.run 的 prompt 与 max_tokens，锁定非流式单消息 seam。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def run(self, prompt: str, *, model_settings: object) -> ResultDouble:
        assert isinstance(model_settings, dict)
        typed_settings = cast(dict[str, object], model_settings)
        max_tokens = typed_settings.get("max_tokens")
        assert max_tokens is None or isinstance(max_tokens, int)
        self.calls.append((prompt, max_tokens))
        return ResultDouble()


def controlled_route() -> tuple[HarnessSettings, ModelRequest, AgentModelPolicy, ModelSettings]:
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        prompt="adapter hello",
        max_output_tokens=17,
    )
    policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    return settings, request, policy, settings.model


class FrozenSnapshotExecutor:
    """在 root ledger 已创建后修改 current settings，证明 invocation 仍走冻结 v2 route。"""

    def __init__(self, settings: HarnessSettings) -> None:
        self._settings = settings

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        del request
        self._settings.model.deployments[
            "real_primary"
        ].base_url = "https://models.example.test/v1/reloaded"
        invocation = cast(
            BoundModelInvocationService,
            context.require_service("model_invocation"),
        )
        response = await invocation.complete(
            ModelRequest(
                deployment_id="real_primary",
                provider="openai-compatible",
                model="fixture-text-1",
                prompt="snapshot route",
                max_output_tokens=8,
            ),
            operation_key="snapshot-route",
        )
        return AgentExecutionResult.completed({"text": response.output_text})

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """该合同无审批分支，任何 resume 都应显式失败。"""

        del request, context, grant
        return AgentExecutionResult.failed("unexpected resume")
