"""Agent scaffold 运行时可用性与 executor 回滚合同测试。"""

from __future__ import annotations

from tests.contracts.test_agent_scaffold_cli_contracts import (
    AgentExecutionContext as AgentExecutionContext,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    AgentExecutionRequest as AgentExecutionRequest,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    AgentRegistry as AgentRegistry,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    Any as Any,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    EvalCaseFactory as EvalCaseFactory,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    EvalRunner as EvalRunner,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    EvalService as EvalService,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    EvalTraceSource as EvalTraceSource,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    EventBus as EventBus,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    IdentityContext as IdentityContext,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    LocalJsonlEventSink as LocalJsonlEventSink,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    Path as Path,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    RunOrchestrator as RunOrchestrator,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    ScaffoldError as ScaffoldError,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    ScoreSink as ScoreSink,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    SQLAlchemyStorage as SQLAlchemyStorage,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    _agents_root as _agents_root,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    _RegistryCaseExecutor as _RegistryCaseExecutor,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    _sqlite_dsn as _sqlite_dsn,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    cast as cast,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    executor_rollback_preflight as executor_rollback_preflight,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    json as json,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    pytest as pytest,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    run_migrations as run_migrations,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    scaffold_agent_package as scaffold_agent_package,
)
from tests.contracts.test_agent_scaffold_cli_contracts import (
    yaml as yaml,
)


@pytest.mark.asyncio
async def test_generated_agent_runs_through_orchestrator_and_manual_eval_gate(
    tmp_path: Path,
) -> None:
    """新生成 agent 必须经真实 orchestrator 可运行，并能通过人工批准后的 eval 数据集闭环。"""

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
    """executor 回滚预检在缺少兼容目标或隔离审计时必须阻断且不改生成包，满足条件后才允许迁移。"""

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
