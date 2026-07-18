"""Agent registry、模型路由与列表 API 合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_registry_model_context_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    _agent_config as _agent_config,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    _asgi_get_json as _asgi_get_json,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    _write_agent_config as _write_agent_config,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    cast as cast,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    create_app as create_app,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    pytest as pytest,
)


def test_api_contract_documents_agent_registry_endpoint() -> None:
    contract = (ROOT / "API-Contract.md").read_text(encoding="utf-8")

    assert "### AGT-001 列出 agents" in contract
    assert "`AgentListResponse`" in contract
    assert "`AgentDescriptor`" in contract
    assert "tests/contracts/test_agent_registry_model_context_contracts.py" in contract
    assert "| `AGT-001` | 规划中 | Agent Registry | `/api/v1/agents` |" not in contract


def test_template_openapi_exposes_agent_list_contract(tmp_path: Path) -> None:
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
    )
    openapi = app.openapi()

    agent_list_operation = openapi["paths"]["/api/v1/agents"]["get"]
    success_schema = agent_list_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert success_schema["$ref"].endswith("/AgentListResponse")

    schemas = openapi["components"]["schemas"]
    descriptor_properties = schemas["AgentDescriptor"]["properties"]
    expected_visible = {
        "agent_id",
        "version",
        "name",
        "description",
        "input_schema_ref",
        "output_schema_ref",
        "config_ref",
        "tool_policy",
        "model_policy",
        "budget",
        "eval_dataset",
        "delegation_targets",
    }
    assert expected_visible <= set(descriptor_properties)
    assert {"config_path", "provider_secret", "callable", "provider_client"}.isdisjoint(
        descriptor_properties
    )
    list_response = schemas["AgentListResponse"]
    assert {"request_id", "agents"} <= set(list_response["properties"])
    assert set(list_response["required"]) == {"request_id", "agents"}
    assert list_response["properties"]["agents"]["items"]["$ref"].endswith("/AgentDescriptor")

    for status in ("409", "422", "500"):
        error_schema = agent_list_operation["responses"][status]["content"]["application/json"][
            "schema"
        ]
        assert error_schema["$ref"].endswith("/ApiErrorEnvelope")


@pytest.mark.asyncio
async def test_agent_list_route_uses_injected_registry_seam(tmp_path: Path) -> None:
    from agent_harness.registry import (
        AgentBudget,
        AgentDescriptor,
        AgentModelPolicy,
        AgentToolPolicy,
    )

    class SpyRegistry:
        def __init__(self) -> None:
            self.calls = 0

        def list_agents(self) -> list[AgentDescriptor]:
            self.calls += 1
            return [
                AgentDescriptor(
                    agent_id="examples.basic",
                    version="0.1.0",
                    name="Basic Example Agent",
                    description="Offline fake model smoke agent.",
                    input_schema_ref="agents.examples.basic.schemas.Input",
                    output_schema_ref="agents.examples.basic.schemas.Output",
                    config_ref="agents/examples/basic/config.yaml",
                    tool_policy=AgentToolPolicy(allowed_tools=[]),
                    model_policy=AgentModelPolicy(
                        provider="fake",
                        default_model="fake-basic",
                        fallback_models=[],
                    ),
                    budget=AgentBudget(
                        max_tokens_per_run=8192,
                        max_cost_usd_per_run=None,
                    ),
                    eval_dataset="eval-cases/drafts/basic.yaml",
                    delegation_targets=[],
                )
            ]

    registry = SpyRegistry()
    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=registry,
    )

    status, body = await _asgi_get_json(cast(Any, app), "/api/v1/agents")

    assert status == 200
    assert registry.calls == 1
    assert body["request_id"] == "req-agents"
    assert body["agents"] == [
        {
            "agent_id": "examples.basic",
            "version": "0.1.0",
            "name": "Basic Example Agent",
            "description": "Offline fake model smoke agent.",
            "input_schema_ref": "agents.examples.basic.schemas.Input",
            "output_schema_ref": "agents.examples.basic.schemas.Output",
            "config_ref": "agents/examples/basic/config.yaml",
            "tool_policy": {"allowed_tools": []},
            "model_policy": {
                "provider": "fake",
                "default_model": "fake-basic",
                "fallback_models": [],
            },
            "budget": {
                "max_tokens_per_run": 8192,
                "max_cost_usd_per_run": None,
            },
            "eval_dataset": "eval-cases/drafts/basic.yaml",
            "delegation_targets": [],
        }
    ]


def test_agent_registry_rejects_duplicate_agent_id(tmp_path: Path) -> None:
    from agent_harness.registry import AgentRegistry, RegistryLoadError

    _write_agent_config(tmp_path, "one", _agent_config("examples.basic"))
    _write_agent_config(tmp_path, "two", _agent_config("examples.basic"))

    with pytest.raises(RegistryLoadError) as exc_info:
        AgentRegistry.load_from_directory(tmp_path)

    error = exc_info.value.error_details[0]
    assert error.code == "registry.duplicate_agent_id"
    assert error.field_path == "agent_id"
    assert "examples.basic" in error.message


def test_agent_registry_rejects_invalid_config(tmp_path: Path) -> None:
    from agent_harness.registry import AgentRegistry, RegistryLoadError

    _write_agent_config(
        tmp_path,
        "invalid",
        """agent_id: examples.invalid
name: Invalid Agent
""",
    )

    with pytest.raises(RegistryLoadError) as exc_info:
        AgentRegistry.load_from_directory(tmp_path)

    error = exc_info.value.error_details[0]
    assert error.code == "registry.invalid_config"
    assert error.field_path is not None


def test_agent_registry_controls_delegation_and_builds_summary(tmp_path: Path) -> None:
    from agent_harness.registry import AgentRegistry

    _write_agent_config(
        tmp_path,
        "source",
        _agent_config("examples.source", delegation_edges=["examples.target"]),
    )
    _write_agent_config(tmp_path, "target", _agent_config("examples.target"))
    registry = AgentRegistry.load_from_directory(tmp_path)

    allowed = registry.check_delegation("examples.source", "examples.target")
    denied = registry.check_delegation("examples.target", "examples.source")
    summary = registry.delegation_summary(
        source_agent_id="examples.source",
        target_agent_id="examples.target",
        parent_run_id="run-parent",
        delegated_run_id="run-child",
        usage_refs=["usage-child"],
        budget_summary={"tokens": 42},
        trace_refs=["trace-child"],
    )

    assert allowed.allowed is True
    assert denied.allowed is False
    assert summary.to_payload() == {
        "parent_agent_id": "examples.source",
        "target_agent_id": "examples.target",
        "parent_run_id": "run-parent",
        "delegated_run_id": "run-child",
        "usage_refs": ["usage-child"],
        "budget_summary": {"tokens": 42},
        "trace_refs": ["trace-child"],
    }


def test_model_router_uses_fake_provider_and_reports_budget_fallback() -> None:
    from agent_harness.models import (
        FakeModelProvider,
        ModelRequest,
        ModelRouter,
        ModelRouterConfig,
    )

    router = ModelRouter(
        config=ModelRouterConfig(
            default_model="fake-basic",
            fallback_models=["fake-small"],
            max_tokens_per_call=20,
            route_max_tokens_per_call={"fake-small": 100},
        ),
        providers={"fake": FakeModelProvider()},
    )

    response = router.route(
        ModelRequest(
            provider="fake",
            prompt="short prompt",
            estimated_input_tokens=3,
            max_output_tokens=2,
        )
    )
    over_budget = router.route(
        ModelRequest(
            provider="fake",
            prompt="this prompt is intentionally over budget",
            estimated_input_tokens=10,
            max_output_tokens=5,
        )
    )

    assert response.decision.action == "call"
    assert response.provider == "fake"
    assert response.model == "fake-basic"
    assert response.output_text.startswith("fake:")
    assert over_budget.decision.action == "fallback"
    assert over_budget.decision.fallback_model == "fake-small"
    assert (
        over_budget.decision.estimated_tokens
        == len(b"this prompt is intentionally over budget") + 5
    )
    assert over_budget.decision.max_tokens == 20


def test_model_router_has_explicit_reload_seam() -> None:
    from agent_harness.models import FakeModelProvider, ModelRouter, ModelRouterConfig

    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic", max_tokens_per_call=100),
        providers={"fake": FakeModelProvider()},
    )
    router.reload(ModelRouterConfig(default_model="fake-reloaded", max_tokens_per_call=100))

    assert router.config.default_model == "fake-reloaded"
