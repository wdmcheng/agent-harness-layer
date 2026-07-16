"""示例 Agent registry、执行器与 CLI 边界合同测试。"""

from __future__ import annotations

from tests.contracts.test_example_agent_flows_contracts import (
    AGENTS as AGENTS,
)
from tests.contracts.test_example_agent_flows_contracts import (
    EXAMPLE_AGENT_IDS as EXAMPLE_AGENT_IDS,
)
from tests.contracts.test_example_agent_flows_contracts import (
    PROFILES as PROFILES,
)
from tests.contracts.test_example_agent_flows_contracts import (
    ROOT as ROOT,
)
from tests.contracts.test_example_agent_flows_contracts import (
    AgentExecutionContext as AgentExecutionContext,
)
from tests.contracts.test_example_agent_flows_contracts import (
    AgentExecutionResult as AgentExecutionResult,
)
from tests.contracts.test_example_agent_flows_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_example_agent_flows_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_example_agent_flows_contracts import (
    Path as Path,
)
from tests.contracts.test_example_agent_flows_contracts import (
    RegistryLoadError as RegistryLoadError,
)
from tests.contracts.test_example_agent_flows_contracts import (
    TestClient as TestClient,
)
from tests.contracts.test_example_agent_flows_contracts import (
    ValidationError as ValidationError,
)
from tests.contracts.test_example_agent_flows_contracts import (
    _dsn as _dsn,
)
from tests.contracts.test_example_agent_flows_contracts import (
    create_app as create_app,
)
from tests.contracts.test_example_agent_flows_contracts import (
    json as json,
)
from tests.contracts.test_example_agent_flows_contracts import (
    pytest as pytest,
)
from tests.contracts.test_example_agent_flows_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_example_agent_flows_contracts import (
    sqlite3 as sqlite3,
)
from tests.contracts.test_example_agent_flows_contracts import (
    subprocess as subprocess,
)
from tests.contracts.test_example_agent_flows_contracts import (
    sys as sys,
)


def test_execution_context_services_are_private_and_results_are_exclusive() -> None:
    """进程内服务不序列化，completed/waiting/failed 不能混入互斥字段。"""

    marker = object()
    context = AgentExecutionContext(
        identity=IdentityContext.local_default(),
        request_id="req-private-services",
    ).bind_services({"marker": marker})

    assert context.require_service("marker") is marker
    assert context.to_payload() == {
        "identity": IdentityContext.local_default().to_payload(),
        "request_id": "req-private-services",
    }
    with pytest.raises(ValidationError):
        AgentExecutionResult(status="completed", output={}, error="must-not-coexist")
    with pytest.raises(ValidationError):
        AgentExecutionResult(status="failed", error="known", output={})


def test_registry_rejects_sync_executor_methods(tmp_path: Path) -> None:
    """仅有同名方法不够；executor 的 run/resume 必须是真正 async callable。"""

    agents_root = tmp_path / "agents"
    package = agents_root / "bad"
    package.mkdir(parents=True)
    (package / "config.yaml").write_text(
        """agent_id: examples.bad
version: 0.1.0
name: Bad Executor
description: Sync methods must fail.
input_schema: agents.bad.schemas.Input
output_schema: agents.bad.schemas.Output
executor: agent:executor
model:
  provider: fake
  default_model: fake
  fallback_models: []
budget:
  max_tokens_per_run: 64
  max_cost_usd_per_run: null
tool_allowlist: []
delegation_edges: []
""",
        encoding="utf-8",
    )
    (package / "schemas.py").write_text(
        """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    value: str = ""
""",
        encoding="utf-8",
    )
    (package / "agent.py").write_text(
        """class Executor:
    def run(self, request, context):
        return None
    def resume(self, request, context, grant):
        return None
executor = Executor()
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistryLoadError, match="async callables"):
        AgentRegistry.load_from_directory(agents_root)


@pytest.mark.parametrize("reference", ["../outside:executor", "/tmp/outside:executor"])
def test_registry_rejects_executor_reference_outside_agent_package(
    tmp_path: Path,
    reference: str,
) -> None:
    """绝对路径和 `..` 都不能把 dynamic import 扩展到 agent package 外。"""

    package = tmp_path / "bad-reference"
    package.mkdir()
    (package / "config.yaml").write_text(
        f"""agent_id: examples.bad_reference
version: 0.1.0
name: Bad Reference
description: Executor escape must fail.
input_schema: bad.Input
output_schema: bad.Output
executor: {reference}
model:
  provider: fake
  default_model: fake
  fallback_models: []
budget:
  max_tokens_per_run: 64
  max_cost_usd_per_run: null
tool_allowlist: []
delegation_edges: []
""",
        encoding="utf-8",
    )

    with pytest.raises(RegistryLoadError) as exc_info:
        AgentRegistry.load_from_directory(tmp_path)
    assert exc_info.value.error_details[0].code == "registry.invalid_executor"


def test_registry_and_cli_expose_four_descriptors_without_executor_leak(tmp_path: Path) -> None:
    """四个示例可发现，public descriptor 不出现 callable/module/path。"""

    registry = AgentRegistry.load_from_directory(AGENTS)
    descriptors = {item.agent_id: item for item in registry.list_agents()}
    assert EXAMPLE_AGENT_IDS <= set(descriptors)
    for agent_id in EXAMPLE_AGENT_IDS:
        descriptor = descriptors[agent_id]
        payload = descriptor.to_payload()
        assert "executor" not in payload and "module" not in payload
        assert descriptor.input_schema_ref.startswith("agents.examples.")
        assert descriptor.output_schema_ref.startswith("agents.examples.")
        assert descriptor.eval_dataset and descriptor.eval_dataset.endswith("/evals")

    agents_list_db = tmp_path / "agents-list.db"
    run_migrations(_dsn(agents_list_db))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "agents",
            "list",
            "--agents-dir",
            str(AGENTS),
            "--profiles-dir",
            str(PROFILES),
            "--storage-dsn",
            _dsn(agents_list_db),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for agent_id in EXAMPLE_AGENT_IDS:
        assert agent_id in result.stdout

    run_db = tmp_path / "ticket-cli.db"
    run_migrations(_dsn(run_db))
    run_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_harness.cli",
            "run",
            "examples.ticket_triage",
            "--profiles-dir",
            str(PROFILES),
            "--agents-dir",
            str(AGENTS),
            "--storage-dsn",
            _dsn(run_db),
            "--events-path",
            str(tmp_path / "ticket-cli-events.jsonl"),
            "--prompt",
            "production outage",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert run_result.returncode == 0, run_result.stderr
    with sqlite3.connect(run_db) as connection:
        output = json.loads(connection.execute("select output_json from agent_runs").fetchone()[0])
    assert output["category"] == "incident"
    assert "fake-ok" not in json.dumps(output)

    api_db = tmp_path / "ticket-api.db"
    run_migrations(_dsn(api_db))
    api = create_app(
        profile="local",
        profiles_dir=PROFILES,
        storage_dsn=_dsn(api_db),
        events_path=tmp_path / "ticket-api-events.jsonl",
    )
    with TestClient(api) as client:
        response = client.post(
            "/api/v1/agents/examples.ticket_triage/runs",
            json={"input": {"text": "login permission denied"}},
            headers={"X-Request-Id": "req-ticket-api"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    with sqlite3.connect(api_db) as connection:
        api_output = json.loads(
            connection.execute("select output_json from agent_runs").fetchone()[0]
        )
    assert api_output["category"] == "access"
