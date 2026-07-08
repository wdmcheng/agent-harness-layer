"""Agent registry、模型上下文和 embedding 的公开契约测试。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from agent_harness.contracts import ErrorDetail
from agent_harness.events import LocalJsonlEventSink
from agent_harness.runtime import RunOrchestrator
from agent_harness.storage import SQLAlchemyStorage, run_migrations
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def _agent_config(agent_id: str, *, delegation_edges: list[str] | None = None) -> str:
    edges = delegation_edges or []
    edge_lines = "\n".join(f"  - {edge}" for edge in edges) or "  []"
    return f"""agent_id: {agent_id}
version: 0.1.0
name: Basic Example Agent
description: Offline fake model smoke agent.
input_schema: agents.examples.basic.schemas.Input
output_schema: agents.examples.basic.schemas.Output
model:
  provider: fake
  default_model: fake-basic
  fallback_models: []
budget:
  max_tokens_per_run: 8192
  max_cost_usd_per_run: null
tool_allowlist: []
eval_dataset: eval-cases/drafts/basic.yaml
delegation_edges:
{edge_lines}
"""


def _write_agent_config(root: Path, relative: str, content: str) -> None:
    path = root / relative / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sqlite_dsn(path: Path) -> str:
    """生成 registry/model/context 合同测试专用 SQLite DSN。"""

    return f"sqlite+aiosqlite:///{path}"


async def _asgi_get_json(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    path: str,
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"x-request-id", b"req-agents")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, status), cast(dict[str, Any], json.loads(body))


async def _asgi_post_json(
    app: Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Any]],
        Awaitable[None],
    ],
    path: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    raw_body = json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw_body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"x-request-id", b"req-run-agent"),
                (b"content-type", b"application/json"),
            ],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return cast(int, status), cast(dict[str, Any], json.loads(response_body))


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
            max_tokens_per_call=5,
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
    assert over_budget.decision.estimated_tokens == 15
    assert over_budget.decision.max_tokens == 5


def test_model_router_has_explicit_reload_seam() -> None:
    from agent_harness.models import FakeModelProvider, ModelRouter, ModelRouterConfig

    router = ModelRouter(
        config=ModelRouterConfig(default_model="fake-basic", max_tokens_per_call=100),
        providers={"fake": FakeModelProvider()},
    )
    router.reload(ModelRouterConfig(default_model="fake-reloaded", max_tokens_per_call=100))

    assert router.config.default_model == "fake-reloaded"


def test_pydantic_ai_adapter_invokes_agent_run_sync_without_leaking_sdk_types() -> None:
    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
    from agent_harness.models import ModelRequest

    pyproject = tomllib.loads((ROOT / "packages" / "agent-harness" / "pyproject.toml").read_text())
    assert "pydantic-ai==2.5.0" in pyproject["project"]["dependencies"]
    assert importlib.util.find_spec("pydantic_ai") is not None

    class Result:
        output = "adapter output"

    class SpyAgent:
        def __init__(self, model: str) -> None:
            self.model = model
            self.prompts: list[str] = []

        def run_sync(self, prompt: str) -> Any:
            self.prompts.append(prompt)
            return Result()

    agents: list[SpyAgent] = []

    def agent_factory(model: str) -> SpyAgent:
        agent = SpyAgent(model)
        agents.append(agent)
        return agent

    response = PydanticAIModelProvider(agent_factory=agent_factory).complete(
        ModelRequest(
            provider="pydantic-ai",
            prompt="hello",
            estimated_input_tokens=3,
            max_output_tokens=5,
        ),
        model="openai:gpt-5.2",
    )

    assert agents[0].model == "openai:gpt-5.2"
    assert agents[0].prompts == ["hello"]
    assert response.provider == "pydantic-ai"
    assert response.output_text == "adapter output"
    assert response.decision.action == "call"
    assert response.token_usage == {"input_tokens": 3, "output_tokens": 2}


@pytest.mark.asyncio
async def test_registry_validation_error_maps_to_api_error_envelope(tmp_path: Path) -> None:
    from agent_harness.registry import RegistryLoadError

    class BrokenRegistry:
        def list_agents(self) -> list[object]:
            raise RegistryLoadError(
                [
                    ErrorDetail(
                        code="registry.invalid_config",
                        message="agent config is invalid",
                        field_path="model.provider",
                    )
                ]
            )

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=BrokenRegistry(),
    )
    status, body = await _asgi_get_json(cast(Any, app), "/api/v1/agents")

    assert status == 422
    assert body == {
        "error": {
            "code": "registry.invalid_config",
            "message": "agent config is invalid",
            "request_id": "req-agents",
            "field_path": "model.provider",
        }
    }


@pytest.mark.asyncio
async def test_agent_run_route_rejects_unknown_agent_before_runtime(tmp_path: Path) -> None:
    from agent_harness.registry import AgentRegistry

    app = create_app(
        orchestrator=cast(RunOrchestrator, object()),
        event_sink=LocalJsonlEventSink(tmp_path / "events.jsonl"),
        registry=AgentRegistry([]),
    )

    status, body = await _asgi_post_json(
        cast(Any, app),
        "/api/v1/agents/does.not.exist/runs",
        {"input": {"prompt": "hello"}},
    )

    assert status == 404
    assert body == {
        "error": {
            "code": "registry.agent_not_found",
            "message": "agent not found: does.not.exist",
            "request_id": "req-run-agent",
            "field_path": "agent_id",
        }
    }


def test_cli_run_rejects_unknown_agent_before_runtime(tmp_path: Path) -> None:
    db_path = tmp_path / "run.db"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "run",
            "does.not.exist",
            "--profiles-dir",
            str(ROOT / "templates" / "service-app" / "configs" / "profiles"),
            "--agents-dir",
            str(ROOT / "templates" / "service-app" / "agents"),
            "--storage-dsn",
            sqlite_dsn(db_path),
            "--events-path",
            str(tmp_path / "events.jsonl"),
        ],
        check=False,
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "registry.agent_not_found: field=agent_id agent not found: does.not.exist" in (
        result.stderr
    )
    assert "run_id:" not in result.stdout


@pytest.mark.asyncio
async def test_context_assembly_and_embedding_cache_are_persisted(tmp_path: Path) -> None:
    from agent_harness.context import ContextAssembler, ContextFragment
    from agent_harness.embeddings import EmbeddingRequest, LocalEmbeddingProvider

    db_path = tmp_path / "context-embedding.db"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    try:
        async with storage.uow() as uow:
            assembly = await ContextAssembler(uow.context_assemblies).assemble(
                tenant_id="default",
                run_id="run-context",
                fragments=[
                    ContextFragment(
                        source_ref="history:1",
                        trust_level="trusted",
                        content="short history",
                        token_estimate=2,
                        kind="history",
                    ),
                    ContextFragment(
                        source_ref="tool:1",
                        trust_level="untrusted",
                        content="tool output " * 20,
                        token_estimate=40,
                        kind="tool_output",
                    ),
                ],
                token_budget=10,
                output_ref="context://run-context/1",
            )
            first = await LocalEmbeddingProvider(
                cache=uow.embedding_cache,
                provider="local",
                model="mock-small",
            ).embed(EmbeddingRequest(input="repeat me"))
            await uow.commit()

        async with storage.uow() as uow:
            stored = await uow.context_assemblies.get(assembly.id)
            second = await LocalEmbeddingProvider(
                cache=uow.embedding_cache,
                provider="local",
                model="mock-small",
            ).embed(EmbeddingRequest(input="repeat me"))
    finally:
        await storage.dispose()

    assert stored is not None
    assert stored.token_budget == 10
    assert stored.output_ref == "context://run-context/1"
    assert stored.truncation_summary["truncated_count"] == 1
    assert stored.truncation_summary["dropped_count"] == 1
    assert stored.truncation_summary["fragment_count"] == 2
    assert assembly.fragment_traces[0].source_ref == "history:1"
    assert assembly.fragment_traces[0].status == "dropped"
    assert assembly.fragment_traces[0].retained_tokens == 0
    assert assembly.fragment_traces[1].source_ref == "tool:1"
    assert assembly.fragment_traces[1].status == "truncated"
    assert assembly.fragment_traces[1].trust_level == "untrusted"
    assert assembly.fragment_traces[1].retained_tokens == 10
    assert assembly.fallback_decision == "trimmed"
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.vector_ref == first.vector_ref


@pytest.mark.asyncio
async def test_openai_compatible_embedding_adapter_posts_and_reuses_cache(tmp_path: Path) -> None:
    from agent_harness.adapters.models.openai_compatible_embeddings import (
        OpenAICompatibleEmbeddingProvider,
    )
    from agent_harness.embeddings import EmbeddingRequest

    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    db_path = tmp_path / "openai-compatible-embedding.db"
    run_migrations(sqlite_dsn(db_path))
    storage = SQLAlchemyStorage.from_dsn(sqlite_dsn(db_path))
    try:
        async with storage.uow() as uow:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = OpenAICompatibleEmbeddingProvider(
                    cache=uow.embedding_cache,
                    base_url="https://embedding.example/v1",
                    model="text-embedding-3-small",
                    api_key="test-key",
                    client=client,
                )
                first = await provider.embed(EmbeddingRequest(input="repeat me"))
                second = await provider.embed(EmbeddingRequest(input="repeat me"))
            await uow.commit()
    finally:
        await storage.dispose()

    assert calls == [
        {
            "url": "https://embedding.example/v1/embeddings",
            "authorization": "Bearer test-key",
            "body": {"model": "text-embedding-3-small", "input": "repeat me"},
        }
    ]
    assert first.provider == "openai-compatible"
    assert first.model == "text-embedding-3-small"
    assert first.vector == [0.1, 0.2, 0.3]
    assert first.cache.hit is False
    assert second.cache.hit is True
    assert second.vector == []
    assert second.vector_ref == first.vector_ref
