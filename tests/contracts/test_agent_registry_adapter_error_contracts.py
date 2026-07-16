"""Agent adapter 与 registry 错误边界合同测试。"""

from __future__ import annotations

import importlib.util

from tests.contracts.test_agent_registry_model_context_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    ErrorDetail as ErrorDetail,
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
    _asgi_get_json as _asgi_get_json,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    _asgi_post_json as _asgi_post_json,
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
from tests.contracts.test_agent_registry_model_context_contracts import (
    sqlite_dsn as sqlite_dsn,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    subprocess as subprocess,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    sys as sys,
)
from tests.contracts.test_agent_registry_model_context_contracts import (
    tomllib as tomllib,
)


def test_pydantic_ai_adapter_invokes_agent_run_sync_without_leaking_sdk_types() -> None:
    from agent_harness.adapters.models.pydantic_ai import PydanticAIModelProvider
    from agent_harness.models import ModelRequest

    pyproject = tomllib.loads((ROOT / "packages" / "agent-harness" / "pyproject.toml").read_text())
    assert "pydantic-ai==2.5.0" in pyproject["project"]["dependencies"]
    assert importlib.util.find_spec("pydantic_ai") is not None

    class Result:
        output = "adapter output"

        class Usage:
            input_tokens = 7
            output_tokens = 4

        def usage(self) -> Usage:
            return self.Usage()

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
    assert response.token_usage == {"input_tokens": 7, "output_tokens": 4}


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
