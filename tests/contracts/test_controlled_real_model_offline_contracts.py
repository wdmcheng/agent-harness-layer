"""默认离线网络哨兵与独立 live smoke evidence 合同。"""

from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.smoke_live_model import LiveSmokeExecutor, run
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import HarnessSettings, load_settings
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    FakeModelProvider,
    ModelProviderInvocationError,
    ModelRequest,
    ModelRouter,
    ModelRouterConfig,
)
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest, RunStatus
from scripts import smoke_live_model

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_default_gates_ignore_provider_credentials_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """provider ambient env 即使存在，local/fake 路由也不读取 secret、不触发 socket。"""

    for name, value in {
        "OPENAI_API_KEY": "ambient-secret",
        "OPENAI_ADMIN_KEY": "ambient-admin",
        "OPENAI_ORG_ID": "ambient-org",
        "OPENAI_PROJECT_ID": "ambient-project",
        "OPENAI_WEBHOOK_SECRET": "ambient-webhook",
        "OPENAI_BASE_URL": "https://evil.example.test/v1",
        "HTTPS_PROXY": "https://proxy.example.test",
    }.items():
        monkeypatch.setenv(name, value)

    def blocked_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("默认离线门禁不得创建网络连接")

    monkeypatch.setattr(socket, "create_connection", blocked_connect)
    settings = load_settings(profile="local", profiles_dir=PROFILES)
    deployment = settings.model.deployments[settings.model.default_deployment_id]
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider=deployment.provider_kind,
            default_model=deployment.default_model,
        ),
        providers={"fake": FakeModelProvider()},
    )
    response = await router.route(
        ModelRequest(provider="fake", prompt="offline", max_output_tokens=2)
    )

    assert response.output_text.startswith("fake:")
    assert settings.model.default_deployment_id == "fake_default"
    assert "ambient-secret" not in settings.model_dump_json()


@pytest.mark.asyncio
async def test_live_smoke_reports_hosted_unverified_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无本会话授权时先于配置/credential/network 返回 hosted-unverified。"""

    monkeypatch.delenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-be-read")
    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 0
    assert payload == {
        "schema_version": "model-live-smoke/v1",
        "status": "hosted-unverified",
        "reason_code": "authorization_missing",
        "provider_called": False,
        "deployment_id": None,
        "provider_kind": None,
        "model": None,
        "endpoint_origin": None,
        "attempt_count": 0,
        "usage": None,
        "latency_ms": None,
    }


@pytest.mark.asyncio
async def test_live_smoke_authorization_controls_do_not_corrupt_typed_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """授权控制键只控制 smoke，不能被误解析为 HarnessSettings 字段。"""

    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", "1")
    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_OPT_IN", "1")

    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 0
    assert payload["status"] == "hosted-unverified"
    assert payload["reason_code"] == "trusted_deployment_missing"
    assert payload["provider_called"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "attempt_count"),
    [
        ("model.provider_failed", 2),
        ("model.provider_retry_exhausted", 2),
        ("model.provider_side_effect_unknown", 1),
    ],
)
async def test_live_smoke_executor_preserves_safe_provider_failure_evidence(
    error_code: str,
    attempt_count: int,
) -> None:
    """429/5xx 重试耗尽与 timeout/unknown 都必须携带安全副作用事实。"""

    class FailingInvocation:
        """模拟正式 facade 封闭 raw provider 异常后的稳定失败。"""

        async def complete(self, *_args: object, **_kwargs: object) -> None:
            raise ModelProviderInvocationError(
                error_code,
                provider_called=True,
                attempt_count=attempt_count,
                latency_ms=37,
            )

    executor = LiveSmokeExecutor(
        ModelRequest(
            deployment_id="real_primary",
            provider="openai-compatible",
            prompt="fixed smoke prompt",
            max_output_tokens=8,
        )
    )
    context = AgentExecutionContext(identity=IdentityContext.local_default()).bind_services(
        {"model_invocation": FailingInvocation()}
    )

    result = await executor.run(
        AgentExecutionRequest(
            agent_id="system.live_model_smoke",
            run_id="run-live-smoke",
            input={},
        ),
        context,
    )

    assert result.status == RunStatus.FAILED.value
    assert executor.error_code == error_code
    assert executor.provider_called is True
    assert executor.attempt_count == attempt_count
    assert executor.latency_ms == 37


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "attempt_count"),
    [
        ("model.provider_failed", 2),
        ("model.provider_retry_exhausted", 2),
        ("model.provider_side_effect_unknown", 1),
    ],
)
async def test_live_smoke_external_blocked_output_keeps_attempt_and_call_facts(
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
    attempt_count: int,
) -> None:
    """公共 run seam 的 external-blocked JSON 不得把已发请求改写成零调用。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=real_model_override(),
    )

    class FailingExecutor:
        """跳过真实 I/O，只提供正式 facade 会暴露的安全失败摘要。"""

        def __init__(self, _request: ModelRequest) -> None:
            self.response = None
            self.error_code = error_code
            self.provider_called = True
            self.attempt_count = attempt_count
            self.latency_ms = 37

        async def run(self, *_args: object, **_kwargs: object) -> Any:
            raise AssertionError("patched orchestrator must not invoke executor")

        async def resume(self, *_args: object, **_kwargs: object) -> Any:
            raise AssertionError("patched orchestrator must not resume executor")

    async def failed_run(*_args: object, **_kwargs: object) -> Any:
        return SimpleNamespace(status=RunStatus.FAILED)

    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", "1")
    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_OPT_IN", "1")

    def loaded_settings(**_kwargs: object) -> HarnessSettings:
        """让公共 run 使用已验证的真实 deployment，但不读取任何外部 secret。"""

        return settings

    monkeypatch.setattr(smoke_live_model, "load_settings", loaded_settings)
    monkeypatch.setattr(smoke_live_model, "LiveSmokeExecutor", FailingExecutor)
    monkeypatch.setattr(smoke_live_model.RunOrchestrator, "start_run", failed_run)

    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 2
    assert payload["status"] == "external-blocked"
    assert payload["reason_code"] == "provider_or_network_blocked"
    assert payload["provider_called"] is True
    assert payload["attempt_count"] == attempt_count
    assert payload["latency_ms"] == 37


def test_live_smoke_gate_is_allowlisted_and_maps_statuses_truthfully() -> None:
    """Make/CI 两端均有独立 producer，hosted-unverified 映射 skipped。"""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "ci_evidence.py").read_text(encoding="utf-8")
    github = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert '"smoke-live-model": "smoke-live-model"' in runner
    assert "ci-smoke-live-model:" in makefile
    assert 'live_status == "hosted-unverified"' in runner
    assert "smoke-live-model:" in github
    assert "smoke-live-model:" in gitlab
    assert "ci-smoke-live-model-${{ github.run_id }}" in github


def test_live_smoke_output_schema_excludes_sensitive_or_content_fields() -> None:
    """机器 schema 只允许安全 route/usage 摘要，不含 prompt/response/header/secret。"""

    source = (ROOT / "scripts" / "smoke_live_model.py").read_text(encoding="utf-8")
    result_fields = source[source.index("return {") : source.index("}\n\n\nasync def run")]
    for forbidden in ("prompt", "response", "header", "secret", "base_url"):
        assert f'"{forbidden}"' not in result_fields


def test_live_smoke_routes_through_configured_fallback_policy() -> None:
    """真实 smoke 不得钉死 primary 或丢弃 deployment 的冻结 fallback。"""

    source = (ROOT / "scripts" / "smoke_live_model.py").read_text(encoding="utf-8")
    request_block = source[source.index("request = ModelRequest(") : source.index("    policy =")]

    assert "model=resolved.default_model" not in request_block
    assert "fallback_models=list(resolved.fallback_models)" in source


def test_live_smoke_cli_accepts_an_explicit_isolated_secret_root() -> None:
    """本地受控 smoke 必须能把 `_FILE` 限定到显式隔离目录，而非搬运密钥。"""

    source = (ROOT / "scripts" / "smoke_live_model.py").read_text(encoding="utf-8")

    assert 'parser.add_argument("--secret-root"' in source
    assert "secret_root=secret_root" in source


def test_live_smoke_contract_failure_preserves_observed_provider_evidence() -> None:
    """terminal fencing 失败不得把已完成的 provider response 改写成零调用。"""

    source = (ROOT / "scripts" / "smoke_live_model.py").read_text(encoding="utf-8")
    branch_start = source.index("if observed_response is not None:")
    branch_end = source.index("provider_kind=observed_response.provider", branch_start)
    observed_response_branch = source[branch_start:branch_end]

    assert "observed_response = executor.response" in source
    assert "provider_called=True" in observed_response_branch
    assert "provider_called=False" not in observed_response_branch
    assert "model=observed_response.model" in source
