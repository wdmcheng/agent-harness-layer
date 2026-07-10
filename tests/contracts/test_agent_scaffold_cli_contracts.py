"""Agent scaffold CLI 的路径、原子发布、runtime 与 eval 合同。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

import agent_harness.scaffold as scaffold_module
from agent_harness.cli import app
from agent_harness.evals import EvalCaseFactory, EvalRunner, EvalService, EvalTraceSource, ScoreSink
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.registry import AgentRegistry
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest, RunOrchestrator
from agent_harness.scaffold import (
    ScaffoldError,
    executor_rollback_preflight,
    scaffold_agent_package,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]


class _RegistryCaseExecutor:
    """把人工批准的 file case 交给生成 executor，而不是复用 expected。"""

    def __init__(self, registry: AgentRegistry, agent_id: str) -> None:
        self._executor = registry.resolve_executor(agent_id)
        self._agent_id = agent_id

    async def execute(self, case: dict[str, Any]) -> dict[str, Any]:
        payload = cast(dict[str, Any], case["payload"])
        input_payload = cast(dict[str, Any], payload["input"])
        result = await self._executor.run(
            AgentExecutionRequest(
                agent_id=self._agent_id,
                run_id="eval-scaffold-run",
                input=input_payload,
            ),
            AgentExecutionContext(identity=IdentityContext.local_default()),
        )
        assert result.status == "completed"
        assert result.output is not None
        return result.output


def _agents_root(tmp_path: Path) -> Path:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    return agents_dir


def _sqlite_dsn(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def test_scaffold_cli_help_success_structure_and_existing_target(tmp_path: Path) -> None:
    help_result = runner.invoke(app, ["scaffold", "agent", "--help"])
    assert help_result.exit_code == 0
    assert "AGENT_ID" in help_result.stdout
    assert "--agents-dir" in help_result.stdout

    agents_dir = _agents_root(tmp_path)
    result = runner.invoke(
        app,
        ["scaffold", "agent", "support.triage", "--agents-dir", str(agents_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "created: support/triage" in result.stdout
    target = agents_dir / "support" / "triage"
    expected_files = {
        "__init__.py",
        "agent.py",
        "tools.py",
        "schemas.py",
        "config.yaml",
        "evals/drafts/example.yaml",
    }
    assert expected_files <= {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    }
    approved_dir = target / "evals" / "approved"
    assert approved_dir.is_dir()
    assert list(approved_dir.iterdir()) == []
    assert (agents_dir / "__init__.py").is_file()
    assert (agents_dir / "support" / "__init__.py").is_file()
    assert not list(target.rglob("__pycache__"))

    registry = AgentRegistry.load_from_directory(agents_dir)
    descriptor = registry.get("support.triage")
    public_payload = descriptor.to_payload()
    assert descriptor.model_policy.provider == "fake"
    assert descriptor.tool_policy.allowed_tools == []
    assert descriptor.delegation_targets == []
    assert descriptor.config_ref == "support/triage/config.yaml"
    assert not {"executor", "callable", "module", "path"} & public_payload.keys()

    list_result = runner.invoke(
        app,
        [
            "agents",
            "list",
            "--agents-dir",
            str(agents_dir),
            "--profiles-dir",
            str(ROOT / "templates" / "service-app" / "configs" / "profiles"),
            "--storage-dsn",
            _sqlite_dsn(tmp_path / "cli-list.db"),
        ],
    )
    assert list_result.exit_code == 0, list_result.output
    assert "support.triage" in list_result.stdout

    source_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in target.rglob("*")
        if path.suffix in {".py", ".yaml"}
    ).lower()
    for forbidden in (
        "pydantic_ai",
        "dbos",
        "logfire",
        "phoenix",
        "langfuse",
        "sqlalchemy",
        "sessionmaker",
        "api_key",
        "secret=",
    ):
        assert forbidden not in source_text

    existing = runner.invoke(
        app,
        ["scaffold", "agent", "support.triage", "--agents-dir", str(agents_dir)],
    )
    assert existing.exit_code == 1
    assert "scaffold.target_exists" in existing.output
    assert AgentRegistry.load_from_directory(agents_dir).get("support.triage") == descriptor


@pytest.mark.parametrize(
    "agent_id",
    ["", "/tmp/agent", "../escape", "support..triage", "Support", "bad-name", "a/b", ".a"],
)
def test_scaffold_invalid_ids_leave_filesystem_unchanged(tmp_path: Path, agent_id: str) -> None:
    agents_dir = _agents_root(tmp_path)
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    result = runner.invoke(
        app,
        ["scaffold", "agent", agent_id, "--agents-dir", str(agents_dir)],
    )
    assert result.exit_code != 0
    assert "scaffold.invalid_agent_id" in result.output
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert after == before


def test_scaffold_rejects_parent_symlink_escape(tmp_path: Path) -> None:
    agents_dir = _agents_root(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (agents_dir / "support").symlink_to(external, target_is_directory=True)

    with pytest.raises(ScaffoldError, match="must not be a symlink") as caught:
        scaffold_agent_package("support.triage", agents_dir=agents_dir)

    assert caught.value.code == "scaffold.symlink_escape"
    assert list(external.iterdir()) == []


def test_staging_is_invisible_and_validation_failure_is_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents_dir = _agents_root(tmp_path)
    observed_staging = False

    def observe_then_validate(root: Path, target: Path, agent_id: str) -> None:
        nonlocal observed_staging
        if root != agents_dir:
            observed_staging = True
            assert AgentRegistry.load_from_directory(agents_dir).list_agents() == []
            staging_namespace = tmp_path / ".agent-harness-scaffold-staging"
            staging_configs = list(staging_namespace.rglob("config.yaml"))
            assert len(staging_configs) == 1
            assert not (agents_dir / "support" / "triage").exists()
        registry = AgentRegistry.load_from_directory(root)
        registry.get(agent_id)
        registry.resolve_executor(agent_id)

    monkeypatch.setattr(scaffold_module, "_validate_generated_package", observe_then_validate)
    scaffold_agent_package("support.triage", agents_dir=agents_dir)
    assert observed_staging is True
    assert not (tmp_path / ".agent-harness-scaffold-staging").exists()

    failing_agents = tmp_path / "failing-agents"
    failing_agents.mkdir()

    def fail_validation(root: Path, target: Path, agent_id: str) -> None:
        del root, target, agent_id
        raise ValueError("injected schema failure")

    monkeypatch.setattr(scaffold_module, "_validate_generated_package", fail_validation)
    with pytest.raises(ScaffoldError, match="injected schema failure"):
        scaffold_agent_package("broken.agent", agents_dir=failing_agents)
    assert list(failing_agents.iterdir()) == []
    assert not (tmp_path / ".agent-harness-scaffold-staging").exists()
    assert not (tmp_path / ".agent-harness-scaffold.lock").exists()


def test_default_discovery_uses_copied_service_app_root_and_unknown_root_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied = tmp_path / "copied-service"
    agents_dir = copied / "agents"
    nested = copied / "docs" / "guide"
    agents_dir.mkdir(parents=True)
    nested.mkdir(parents=True)
    (copied / "pyproject.toml").write_text(
        '[project]\nname = "agent-harness-service-app"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(nested)
    result = runner.invoke(app, ["scaffold", "agent", "support.triage"])
    assert result.exit_code == 0, result.output
    assert (agents_dir / "support" / "triage" / "config.yaml").is_file()
    assert not (copied / "templates" / "service-app" / "agents").exists()

    unknown = tmp_path / "unknown"
    unknown.mkdir()
    monkeypatch.chdir(unknown)
    before = sorted(path.relative_to(unknown).as_posix() for path in unknown.rglob("*"))
    failed = runner.invoke(app, ["scaffold", "agent", "unknown.agent"])
    assert failed.exit_code == 1
    assert "scaffold.agents_dir_required" in failed.output
    assert "--agents-dir" in failed.output
    after = sorted(path.relative_to(unknown).as_posix() for path in unknown.rglob("*"))
    assert after == before


def test_explicit_custom_agents_dir_emits_importable_schema_refs(
    tmp_path: Path,
) -> None:
    agents_dir = tmp_path / "custom-root"
    agents_dir.mkdir()
    scaffold_agent_package("support.triage", agents_dir=agents_dir)
    descriptor = AgentRegistry.load_from_directory(agents_dir).get("support.triage")

    assert descriptor.input_schema_ref == "custom-root.support.triage.schemas.ScaffoldInput"
    assert descriptor.output_schema_ref == "custom-root.support.triage.schemas.ScaffoldOutput"
    assert descriptor.eval_dataset == "custom-root/support/triage/evals/approved"

    module_name = "custom-root.support.triage.schemas"
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        assert hasattr(module, "ScaffoldInput")
        assert hasattr(module, "ScaffoldOutput")
    finally:
        sys.path.remove(str(tmp_path))
        for loaded_name in tuple(sys.modules):
            if loaded_name == "custom-root" or loaded_name.startswith("custom-root."):
                sys.modules.pop(loaded_name, None)


@pytest.mark.asyncio
async def test_generated_agent_runs_through_orchestrator_and_manual_eval_gate(
    tmp_path: Path,
) -> None:
    agents_dir = _agents_root(tmp_path)
    created = scaffold_agent_package("support.triage", agents_dir=agents_dir)
    registry = AgentRegistry.load_from_directory(agents_dir)
    dsn = _sqlite_dsn(tmp_path / "runtime.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    events_path = tmp_path / "events.jsonl"
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=LocalJsonlEventSink(events_path)),
        executor_resolver=registry.resolve_executor,
    )
    try:
        result = await orchestrator.start_run(
            agent_id="support.triage",
            input={"prompt": "real executor"},
            trace_id="trace-scaffold-runtime",
        )
        assert result.status.value == "completed"
        assert result.terminal_event == "run.completed"

        draft_document = cast(
            dict[str, Any],
            yaml.safe_load(
                (created.target_dir / "evals" / "drafts" / "example.yaml").read_text(
                    encoding="utf-8"
                )
            ),
        )
        payload = cast(dict[str, Any], draft_document["payload"])
        identity = IdentityContext.local_default()
        scores_path = tmp_path / "scores.jsonl"
        eval_service = EvalService(
            storage=storage,
            factory=EvalCaseFactory(),
            score_sink=ScoreSink(local_path=scores_path),
            drafts_dir=created.target_dir / "evals" / "drafts",
            approved_dir=created.target_dir / "evals" / "approved",
        )
        draft = await eval_service.draft_from_trace(
            EvalTraceSource(
                tenant_id=identity.tenant_id,
                agent_id="support.triage",
                trace_id="trace-scaffold-eval",
                trigger="manual",
                input=cast(dict[str, Any], payload["input"]),
                expected=cast(dict[str, Any], payload["expected"]),
            )
        )
        assert draft.status == "draft"
        approved = await eval_service.approve_case(
            actor=identity,
            case_id=draft.case_id,
            reason="人工确认 scaffold 结果",
            dataset="default",
        )
        assert approved.case.status == "approved"
        eval_result = await EvalRunner(score_sink=eval_service.score_sink).run_file_dataset(
            dataset_dir=created.target_dir / "evals",
            tenant_id=identity.tenant_id,
            agent_id="support.triage",
            case_executor=_RegistryCaseExecutor(registry, "support.triage"),
        )
        assert eval_result.status == "completed"
        assert eval_result.case_count == 1
        assert eval_result.skipped_drafts == 0
        assert eval_result.score_summary["passed"] == 1
        assert eval_result.local_refs
        assert (created.target_dir / "evals" / "drafts" / "example.yaml").is_file()
        score_evidence = scores_path.read_text(encoding="utf-8")
        assert "trace-scaffold-eval" in score_evidence
    finally:
        await storage.dispose()

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    terminal_events = [event for event in events if event.get("terminal") is True]
    assert len(terminal_events) == 1
    assert terminal_events[0]["event_type"] == "run.completed"


@pytest.mark.asyncio
async def test_executor_rollback_preflight_blocks_without_mutating_generated_agent(
    tmp_path: Path,
) -> None:
    agents_dir = _agents_root(tmp_path)
    created = scaffold_agent_package("support.triage", agents_dir=agents_dir)
    before = {
        path.relative_to(created.target_dir).as_posix(): path.read_bytes()
        for path in created.target_dir.rglob("*")
        if path.is_file()
    }

    with pytest.raises(ScaffoldError, match="support.triage") as blocked:
        executor_rollback_preflight([agents_dir])
    assert blocked.value.code == "scaffold.executor_rollback_blocked"
    after = {
        path.relative_to(created.target_dir).as_posix(): path.read_bytes()
        for path in created.target_dir.rglob("*")
        if path.is_file()
    }
    assert after == before

    registry = AgentRegistry.load_from_directory(agents_dir)
    executor = registry.resolve_executor("support.triage")
    execution = await executor.run(
        AgentExecutionRequest(
            agent_id="support.triage",
            run_id="rollback-proof",
            input={"prompt": "still runnable"},
        ),
        AgentExecutionContext(identity=IdentityContext.local_default()),
    )
    assert execution.status == "completed"
    assert execution.output is not None
    assert execution.output["result"] == "scaffold-ready"

    with pytest.raises(ScaffoldError, match="support.triage"):
        executor_rollback_preflight(
            [agents_dir],
            target_supported_executor_refs=["target_runtime:executor"],
        )

    config_path = created.target_dir / "config.yaml"
    config_text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        config_text.replace("executor: agent:executor", "executor: target_runtime:executor"),
        encoding="utf-8",
    )
    migrated = executor_rollback_preflight(
        [agents_dir],
        target_supported_executor_refs=["target_runtime:executor"],
    )
    assert migrated.allowed is True
    with pytest.raises(ScaffoldError, match="support.triage"):
        executor_rollback_preflight(
            [agents_dir],
            isolated_agent_audit_refs={"support.triage": "audit://isolation/1"},
        )

    isolated_root = tmp_path / "isolated-agents"
    isolated_root.mkdir()
    (agents_dir / "support").rename(isolated_root / "support")
    isolated = executor_rollback_preflight(
        [agents_dir],
        isolated_agent_audit_refs={"support.triage": "audit://isolation/1"},
    )
    assert isolated.allowed is True
    assert (isolated_root / "support" / "triage" / "config.yaml").is_file()
