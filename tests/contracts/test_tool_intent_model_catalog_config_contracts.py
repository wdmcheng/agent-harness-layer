"""Tool-intent model-catalog/v2、请求身份与静态预算合同。"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from tests.contracts.test_controlled_real_model_config_contracts import (
    PROFILES,
    real_model_override,
)

from agent_harness.config import SettingsLoadError, load_settings
from agent_harness.config.model_catalog import model_catalog_digest
from agent_harness.config.model_endpoints import resolve_model_deployment
from agent_harness.models import ModelRequest, ModelRouter, ModelRouterConfig
from agent_harness.models.router import ModelRouteError
from agent_harness.models.structured import compile_output_schema_definition
from agent_harness.models.tool_catalog import (
    ToolIntentRequestIdentity,
    build_tool_catalog,
)
from agent_harness.models.tool_intent import ToolCatalogSourceDescriptor
from agent_harness.registry import AgentModelPolicy


def tool_intent_override() -> dict[str, object]:
    """把现有离线真实配置收窄为singleton tool-intent deployment。"""

    overrides = deepcopy(real_model_override())
    model = overrides["model"]  # type: ignore[index]
    catalog = model["model_catalogs"]["fixture_text_1"]  # type: ignore[index]
    catalog.update(  # type: ignore[union-attr]
        {
            "version": "v2",
            "request_shape_ref": "single-user-text-with-tool-catalog",
            "max_tool_catalog_utf8_bytes": 512,
        }
    )
    catalog["digest"] = model_catalog_digest("fixture_text_1", catalog)  # type: ignore[index]
    deployment = model["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(  # type: ignore[union-attr]
        {
            "model_catalog_versions": {"fixture-text-1": "v2"},
            "fallback_models": [],
            "max_attempts": 1,
            "retryable_http_statuses": [],
            "cross_provider_failover_http_statuses": [],
            "completion_classifier_ref": None,
            "completion_classifier_version": None,
            "max_per_attempt_token_bound": 1680,
            "max_per_attempt_cost_bound": "0.001808",
            "capabilities": ["tool_intent"],
            "max_structured_repair_attempts": 0,
        }
    )
    return overrides


def test_tool_intent_catalog_v2_resolves_exact_static_ceiling() -> None:
    """静态上界必须计入prompt cap、catalog cap、envelope和deployment output cap。"""

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    )
    resolved = resolve_model_deployment(settings.model, "real_primary")
    catalog = resolved.model_catalogs["fixture-text-1"]

    assert catalog.version == "v2"
    assert catalog.request_shape_ref == "single-user-text-with-tool-catalog"
    assert catalog.max_tool_catalog_utf8_bytes == 512
    assert resolved.max_per_attempt_token_bound == 1024 + 512 + 16 + 128


@pytest.mark.parametrize(
    "mutation",
    [
        {"capabilities": ["tool_intent", "text_completion"]},
        {"fallback_models": ["fixture-text-1"]},
        {"max_attempts": 2},
        {"retryable_http_statuses": [429]},
        {"cross_provider_failover_http_statuses": [503]},
        {"max_structured_repair_attempts": 1},
    ],
)
def test_tool_intent_deployment_rejects_retry_fallback_or_mixed_protocol(
    mutation: dict[str, object],
) -> None:
    """首个tool-intent route固定单capability、单route、单attempt且无repair/retry。"""

    overrides = tool_intent_override()
    deployment = overrides["model"]["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(mutation)  # type: ignore[union-attr]

    with pytest.raises(SettingsLoadError):
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def test_catalog_v1_and_v2_fields_cannot_be_mixed_or_omitted() -> None:
    """No-tools v1禁止catalog cap，with-tools v2必须显式提供非负cap。"""

    with_cap_on_v1 = real_model_override()
    v1_catalog = with_cap_on_v1["model"]["model_catalogs"]["fixture_text_1"]  # type: ignore[index]
    v1_catalog["max_tool_catalog_utf8_bytes"] = 1  # type: ignore[index]
    v1_catalog["digest"] = model_catalog_digest("fixture_text_1", v1_catalog)  # type: ignore[index]
    with pytest.raises(SettingsLoadError):
        load_settings(profile="local", profiles_dir=PROFILES, overrides=with_cap_on_v1)

    missing_cap_on_v2 = tool_intent_override()
    v2_catalog = missing_cap_on_v2["model"]["model_catalogs"]["fixture_text_1"]  # type: ignore[index]
    v2_catalog.pop("max_tool_catalog_utf8_bytes")  # type: ignore[union-attr]
    v2_catalog["digest"] = model_catalog_digest("fixture_text_1", v2_catalog)  # type: ignore[index]
    with pytest.raises(SettingsLoadError):
        load_settings(profile="local", profiles_dir=PROFILES, overrides=missing_cap_on_v2)


def test_tool_intent_catalog_bounds_reject_checked_integer_overflow() -> None:
    """所有持久化预算整数必须在受信公式执行前落入 signed BIGINT。"""

    overrides = tool_intent_override()
    model = overrides["model"]  # type: ignore[index]
    catalog = model["model_catalogs"]["fixture_text_1"]  # type: ignore[index]
    catalog.update(  # type: ignore[union-attr]
        {
            "max_tool_catalog_utf8_bytes": 2**63,
            "cost_enabled": False,
            "input_token_price_usd": None,
            "output_token_price_usd": None,
            "price_source_ref": None,
            "price_source_version": None,
        }
    )
    catalog["digest"] = model_catalog_digest("fixture_text_1", catalog)  # type: ignore[index]
    deployment = model["deployments"]["real_primary"]  # type: ignore[index]
    deployment.update(  # type: ignore[union-attr]
        {
            "max_per_attempt_token_bound": 2**63 + 1024 + 16 + 128,
            "max_per_attempt_cost_bound": None,
        }
    )

    with pytest.raises(SettingsLoadError):
        load_settings(profile="local", profiles_dir=PROFILES, overrides=overrides)


def test_tool_request_identity_is_exact_and_rejects_bool_or_catalog_overflow() -> None:
    """Route、approval与replay共同消费同一exact request identity。"""

    identity = ToolIntentRequestIdentity(
        model_catalog_digest="1" * 64,
        tool_catalog_digest="2" * 64,
        tool_catalog_utf8_bytes=352,
        max_tool_catalog_utf8_bytes=512,
        trusted_input_token_bound=400,
        output_token_cap=16,
    )

    assert set(identity.model_dump(mode="json")) == {
        "schema_version",
        "request_shape_ref",
        "request_shape_version",
        "model_catalog_digest",
        "tool_catalog_digest",
        "tool_catalog_utf8_bytes",
        "max_tool_catalog_utf8_bytes",
        "trusted_input_token_bound",
        "output_token_cap",
    }
    assert len(identity.digest) == 64
    for mutation in (
        {"tool_catalog_utf8_bytes": True},
        {"tool_catalog_utf8_bytes": 513},
        {"extra": "forbidden"},
    ):
        payload = identity.model_dump(mode="python")
        payload.update(mutation)
        with pytest.raises(ValidationError):
            ToolIntentRequestIdentity.model_validate(payload)


def _tool_catalog():
    schema = compile_output_schema_definition(
        {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        schema_ref="search-input",
        version="v1",
    )
    return build_tool_catalog(
        allowed_tools=("search",),
        registry_descriptors=(
            ToolCatalogSourceDescriptor(
                name="search",
                action="tool.search",
                resource="tool:search",
                input_schema=schema,
                registry_ordinal=0,
            ),
        ),
        selection=None,
    )


def _router_and_policy(*, overrides: dict[str, object] | None = None):
    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=overrides or tool_intent_override(),
    )

    async def prepare_tool_intent(*_args: object, **_kwargs: object) -> None:
        """纯规划夹具只需声明零执行 observation protocol，不会实际调用。"""

    provider = SimpleNamespace(
        provider_id="openai-compatible",
        tool_intent_observation_supported=True,
        prepare_tool_intent=prepare_tool_intent,
    )
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": cast(Any, provider)},
        model_settings=settings.model,
    )
    policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )
    return router, policy


def tool_intent_catalog_fixture():
    """向跨模块adapter合同暴露合法catalog，避免依赖本文件的私有测试实现。"""

    return _tool_catalog()


def tool_intent_router_and_policy_fixture():
    """向跨模块adapter合同暴露合法tool-intent规划上下文。"""

    return _router_and_policy()


def test_tool_intent_route_binds_actual_catalog_bytes_and_dynamic_budget() -> None:
    """实际catalog bytes进入trusted input、reservation与公开request identity。"""

    router, policy = _router_and_policy()
    plan = router.plan_tool_intent(
        ModelRequest(
            deployment_id="real_primary",
            provider="openai-compatible",
            model="fixture-text-1",
            prompt="hello",
            capability="tool_intent",
            max_output_tokens=8,
        ),
        tool_catalog=_tool_catalog(),
        agent_policy=policy,
    )

    assert plan.request_shape_ref == "single-user-text-with-tool-catalog"
    assert plan.tool_request_identity is not None
    assert plan.tool_request_identity.tool_catalog_utf8_bytes == 352
    assert plan.tool_request_identity.max_tool_catalog_utf8_bytes == 512
    assert plan.trusted_input_token_bound == len(b"hello") + 352 + 16
    assert plan.per_attempt_token_bound == len(b"hello") + 352 + 16 + 8
    assert plan.reserved_token_bound == plan.per_attempt_token_bound
    assert plan.tool_request_identity_digest == plan.tool_request_identity.digest


def test_tool_intent_route_requires_dedicated_catalog_aware_planner() -> None:
    """通用no-tools planner不得接收tool intent，也不能给文本请求注入catalog。"""

    router, policy = _router_and_policy()
    request = ModelRequest(
        deployment_id="real_primary",
        provider="openai-compatible",
        model="fixture-text-1",
        prompt="hello",
        capability="tool_intent",
        max_output_tokens=8,
    )
    with pytest.raises(ValueError) as missing_catalog:
        router.plan(request, agent_policy=policy)
    assert getattr(missing_catalog.value, "code", None) == "model.tool_catalog_conflict"
    with pytest.raises(ValueError) as wrong_capability:
        router.plan_tool_intent(
            request.model_copy(update={"capability": "text_completion"}),
            tool_catalog=_tool_catalog(),
            agent_policy=policy,
        )
    assert getattr(wrong_capability.value, "code", None) == "model.tool_catalog_conflict"


def test_tool_intent_route_rejects_provider_without_zero_execution_observation() -> None:
    """Provider protocol 必须在 usage/client seam 前证明不会注册 executable callback。"""

    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider

    agent_factory_calls = 0

    def agent_factory(_plan: object) -> object:
        nonlocal agent_factory_calls
        agent_factory_calls += 1
        return object()

    settings = load_settings(
        profile="local",
        profiles_dir=PROFILES,
        overrides=tool_intent_override(),
    )
    provider = PydanticAIModelProvider(agent_factory=cast(Any, agent_factory))
    router = ModelRouter(
        config=ModelRouterConfig(
            default_provider="openai-compatible",
            default_model="fixture-text-1",
        ),
        providers={"openai-compatible": provider},
        model_settings=settings.model,
    )
    policy = AgentModelPolicy(
        deployment_id="real_primary",
        provider="openai-compatible",
        allowed_models=["fixture-text-1"],
        default_model="fixture-text-1",
        fallback_models=[],
    )

    with pytest.raises(ModelRouteError) as rejected:
        router.plan_tool_intent(
            ModelRequest(
                deployment_id="real_primary",
                provider="openai-compatible",
                model="fixture-text-1",
                prompt="hello",
                capability="tool_intent",
                max_output_tokens=8,
            ),
            tool_catalog=_tool_catalog(),
            agent_policy=policy,
        )

    assert rejected.value.code == "model.capability_unsupported"
    assert agent_factory_calls == 0
