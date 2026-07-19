"""Core executor、registry 与 context assembly 合同测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_harness.artifacts import FileArtifactStore
from agent_harness.context import ContextAssemblyService, ContextFragment
from agent_harness.events import EventBus, LocalJsonlEventSink
from agent_harness.registry import AgentRegistry, RegistryLoadError
from agent_harness.runtime import (
    AgentExecutionContext,
    AgentExecutionRequest,
    AgentExecutionResult,
    ApprovalGrant,
    RunOrchestrator,
    RunStatus,
)
from agent_harness.storage import SQLAlchemyStorage, run_migrations


def sqlite_dsn(path: Path) -> str:
    """将临时数据库路径转换为异步 SQLite DSN，供真实迁移与 UoW 场景复用。"""

    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_context_assembly_output_ref_contains_budgeted_model_input(tmp_path: Path) -> None:
    """output_ref 必须证明实际组装输出，不能把未裁剪输入伪装成模型输入。"""

    dsn = sqlite_dsn(tmp_path / "context-output.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    artifacts = FileArtifactStore(tmp_path / "context-artifacts")
    try:
        result = await ContextAssemblyService(
            storage=storage,
            artifact_store=artifacts,
        ).assemble(
            tenant_id="default",
            run_id=None,
            fragments=[
                ContextFragment(
                    source_ref="fixture://long",
                    trust_level="untrusted",
                    content="ABCDEFGHIJ",
                    token_estimate=10,
                    kind="retrieval",
                )
            ],
            token_budget=1,
        )
        async with storage.uow() as uow:
            persisted = await uow.context_assemblies.get(result.id)
    finally:
        await storage.dispose()

    evidence = artifacts.read_json(result.output_ref)
    assert result.assembled_text == "A"
    assert evidence["kind"] == "context-assembly-output"
    assert evidence["assembled_text"] == result.assembled_text
    assert evidence["retained_fragments"] == [
        {
            "source_ref": "fixture://long",
            "trust_level": "untrusted",
            "content": "A",
            "token_estimate": 1,
            "kind": "retrieval",
            "priority": 100,
        }
    ]
    assert "ABCDEFGHIJ" not in json.dumps(evidence)
    assert persisted is not None and persisted.output_ref == result.output_ref


def _config(agent_id: str, executor: str = "executor:executor") -> str:
    """渲染最小有效 agent 配置，仅暴露被 registry 合同覆盖的变量字段。"""

    return f"""agent_id: {agent_id}
version: 0.1.0
name: Executor Contract Agent
description: Controlled executor fixture.
input_schema: fixture.Input
output_schema: fixture.Output
executor: {executor}
model:
  provider: fake
  default_model: fake
  fallback_models: []
budget:
  max_tokens_per_run: 128
  max_cost_usd_per_run: null
tool_allowlist: []
delegation_edges: []
"""


def _write_agent(root: Path, name: str, *, config: str, module: str | None = None) -> Path:
    """创建可由 registry 真实加载的临时 agent 包，并按需写入 executor 模块。"""

    package = root / name
    package.mkdir(parents=True)
    rendered = config.replace("fixture.Input", f"{root.name}.{name}.schemas.Input").replace(
        "fixture.Output", f"{root.name}.{name}.schemas.Output"
    )
    (package / "config.yaml").write_text(rendered, encoding="utf-8")
    (package / "schemas.py").write_text(
        """from agent_harness.contracts.dto import HarnessDTO

class Input(HarnessDTO):
    value: str = ""

class Output(HarnessDTO):
    value: str = ""
""",
        encoding="utf-8",
    )
    if module is not None:
        (package / "executor.py").write_text(module, encoding="utf-8")
    return package


def test_registry_resolves_package_local_executor_without_public_leak(tmp_path: Path) -> None:
    """验证 registry 能加载包内 executor，同时 descriptor 不泄露私有 callable 细节。"""

    module = """
from agent_harness.runtime import AgentExecutionResult

class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({"echo": request.input})
    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({"approval_id": grant.approval_id})

executor = Executor()
"""
    _write_agent(tmp_path, "good", config=_config("examples.good"), module=module)

    registry = AgentRegistry.load_from_directory(tmp_path)
    descriptor = registry.get("examples.good")
    executor = registry.resolve_executor("examples.good")
    request = AgentExecutionRequest(agent_id="examples.good", run_id="run-1", input={})

    assert "executor" not in descriptor.to_payload()
    assert "module" not in descriptor.to_payload()
    assert callable(executor.run)
    assert request.agent_id == "examples.good"


def test_registry_validates_all_references_before_importing_any_target(tmp_path: Path) -> None:
    """验证任一引用无效时先整体校验，绝不提前 import 其他 agent 的副作用模块。"""

    marker = tmp_path / "imported.txt"
    module = f"""
from pathlib import Path
from agent_harness.runtime import AgentExecutionResult
Path({str(marker)!r}).write_text("imported")
class Executor:
    async def run(self, request, context):
        return AgentExecutionResult.completed({{}})
    async def resume(self, request, context, grant):
        return AgentExecutionResult.completed({{}})
executor = Executor()
"""
    _write_agent(tmp_path, "a-valid", config=_config("examples.valid"), module=module)
    _write_agent(
        tmp_path,
        "z-invalid",
        config=_config("examples.invalid", executor="missing:executor"),
    )

    with pytest.raises(RegistryLoadError) as exc_info:
        AgentRegistry.load_from_directory(tmp_path)

    assert exc_info.value.error_details[0].field_path == "executor"
    assert not marker.exists()


class _StaticExecutor:
    """始终返回指定结果的 typed executor 桩，用于验证 orchestrator 不再兜底 fake。"""

    def __init__(self, result: AgentExecutionResult) -> None:
        """固定 run/resume 共用的执行结果，令场景只覆盖编排层状态映射。"""

        self.result = result

    async def run(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
    ) -> AgentExecutionResult:
        """忽略传入运行上下文并返回预设结果，满足 typed executor 协议。"""

        del request, context
        return self.result

    async def resume(
        self,
        request: AgentExecutionRequest,
        context: AgentExecutionContext,
        grant: ApprovalGrant,
    ) -> AgentExecutionResult:
        """忽略恢复参数并返回预设结果，验证 resume 也不走 fake fallback。"""

        del request, context, grant
        return self.result


@pytest.mark.asyncio
async def test_orchestrator_uses_typed_executor_and_has_no_fake_fallback(tmp_path: Path) -> None:
    """验证存在 executor 时写入真实完成结果，缺失时稳定失败而非生成伪成功。"""

    dsn = sqlite_dsn(tmp_path / "runtime.db")
    run_migrations(dsn)
    storage = SQLAlchemyStorage.from_dsn(dsn)
    sink = LocalJsonlEventSink(tmp_path / "events.jsonl")
    completed_executor = _StaticExecutor(AgentExecutionResult.completed({"answer": 42}))
    orchestrator = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
        executor_resolver=lambda _agent_id: completed_executor,
    )
    missing = RunOrchestrator(
        storage=storage,
        event_bus=EventBus(sink=sink),
    )
    try:
        completed = await orchestrator.start_run(agent_id="examples.real", input={"x": 1})
        failed = await missing.start_run(agent_id="examples.missing", input={})
        async with storage.uow() as uow:
            completed_row = await uow.runs.get(completed.run_id)
            failed_row = await uow.runs.get(failed.run_id)
    finally:
        await storage.dispose()

    assert completed.status == RunStatus.COMPLETED
    assert completed_row is not None and completed_row.output == {"answer": 42}
    assert failed.status == RunStatus.FAILED
    assert failed_row is not None
    assert failed_row.output is None
    assert failed_row.error == {"reason": "agent executor is not configured"}
    assert "fake-ok" not in json.dumps(failed_row.to_payload())
