"""CLI 业务 input 与可信 provenance 分离的聚焦合同。"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.contracts.runtime_contract_helpers import sqlite_dsn
from typer.testing import CliRunner

from agent_harness import cli as runtime_cli
from agent_harness.identity import IdentityContext
from agent_harness.models import FakeModelProvider, ModelRequest
from agent_harness.policy.engine import InputGuardrail, PolicyEngine, YamlPolicyProvider
from agent_harness.runtime import AgentExecutionContext, RunOrchestrator, RunQueue, RunQueueMessage
from agent_harness.runtime._continuation_context import (
    PROVENANCE_SCHEMA_VERSION,
    RunInputProvenance,
    bind_execution_provenance,
    classify_execution_context_record,
    execution_context_payload,
    execution_provenance,
)
from agent_harness.storage import RunRecord, run_migrations
from agent_harness.storage.repositories import RunExecutionContextRecord
from app.api.routes.run_support import (
    AgentRunCreateRequest,
    RunCreateResponse,
    RunDetailResponse,
    RunEventsResponse,
    RunResumeRequest,
)
from app.main import create_app


def test_provenance_is_closed_private_and_absent_from_public_runtime_schema() -> None:
    provenance = RunInputProvenance(source="cli")
    assert provenance.model_dump() == {"source": "cli"}
    with pytest.raises(ValueError):
        RunInputProvenance.model_validate({"source": "api"})
    with pytest.raises(ValueError):
        RunInputProvenance.model_validate({"source": "cli", "extra": True})

    import agent_harness.runtime as runtime

    assert not hasattr(runtime, "RunInputProvenance")
    assert "provenance" not in AgentExecutionContext.model_fields
    assert "input_provenance" not in AgentExecutionContext.model_fields
    context = AgentExecutionContext(
        identity=IdentityContext.local_default(),
        request_id="request-1",
        trace_id="trace-1",
    )
    bind_execution_provenance(context, provenance)
    assert execution_provenance(context) == provenance
    assert "provenance" not in context.model_dump_json()


def test_public_orchestrator_signatures_do_not_expose_provenance() -> None:
    for method_name in ("start_run", "submit_run", "resume_run"):
        parameters = inspect.signature(getattr(RunOrchestrator, method_name)).parameters
        assert "provenance" not in parameters
        assert "current_resume_request_id" not in parameters

    assert "provenance" in inspect.signature(InputGuardrail.check).parameters


def test_public_run_http_openapi_and_repository_shapes_do_not_expose_provenance() -> None:
    """逐值冻结公开 run DTO、repository 摘要与 OpenAPI 字段集合。"""

    expected_fields = {
        AgentRunCreateRequest: {"input", "idempotency_key"},
        RunCreateResponse: {"request_id", "run_id", "status", "terminal_event"},
        RunDetailResponse: {
            "request_id",
            "run_id",
            "agent_id",
            "status",
            "terminal_event",
            "parent_run_id",
            "delegation_summary",
        },
        RunEventsResponse: {"request_id", "events"},
        RunResumeRequest: {"resume_token"},
        RunRecord: {
            "tenant_id",
            "session_id",
            "agent_id",
            "idempotency_key",
            "parent_run_id",
            "trace_id",
            "input",
            "id",
            "status",
            "output",
            "error",
        },
    }
    for model, fields in expected_fields.items():
        assert set(model.model_fields) == fields
        assert not {"provenance", "input_provenance"} & set(model.model_fields)

    schema = create_app(orchestrator=cast(Any, object()), event_sink=cast(Any, object())).openapi()
    run_schemas = {
        name: value
        for name, value in schema["components"]["schemas"].items()
        if name
        in {
            "AgentRunCreateRequest",
            "RunCreateResponse",
            "RunDetailResponse",
            "RunEventsResponse",
            "RunResumeRequest",
        }
    }
    serialized = json.dumps(run_schemas, ensure_ascii=False, sort_keys=True)
    assert "provenance" not in serialized
    assert "input_provenance" not in serialized


def test_public_queue_message_and_protocol_shapes_remain_exact() -> None:
    """冻结既有 queue DTO 与 consumer ownership 方法签名，不把 provenance 升格为协议。"""

    assert set(RunQueueMessage.model_fields) == {
        "schema_version",
        "kind",
        "request_id",
        "operation_id",
        "idempotency_key",
        "tenant_id",
        "run_id",
        "approval_id",
        "resolution_lease_id",
    }
    expected_parameters = {
        "pickup": {"self", "consumer_id", "block_milliseconds"},
        "reclaim": {"self", "consumer_id", "min_idle_seconds"},
        "ack": {"self", "receipt"},
    }
    for method_name, names in expected_parameters.items():
        assert set(inspect.signature(getattr(RunQueue, method_name)).parameters) == names

    message = RunQueueMessage(
        kind="execute_run",
        request_id="queue-delivery",
        operation_id="run:run-1:execute",
        idempotency_key="queue-key",
        tenant_id="tenant-1",
        run_id="run-1",
    )
    serialized = message.model_dump_json()
    assert "provenance" not in serialized
    assert "input_provenance" not in serialized


@pytest.mark.parametrize("execution_request_id", ["request-1", None])
def test_execution_context_envelope_is_exact_and_request_id_is_authoritative(
    execution_request_id: str | None,
) -> None:
    payload = execution_context_payload(
        identity={"tenant_id": "tenant"},
        request_id=execution_request_id,
        trace_id="trace-1",
        checkpoint_state=None,
        provenance=RunInputProvenance(source="cli"),
    )
    assert payload["input_provenance"] == {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source": "cli",
        "execution_request_id": execution_request_id,
    }
    classified = classify_execution_context_record(
        RunExecutionContextRecord(run_id="run-1", execution_context=payload)
    )
    assert classified.provenance == RunInputProvenance(source="cli")
    assert classified.authoritative_request_id == execution_request_id


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"provenance": {"source": "cli"}},
        {"request_id": "", "input_provenance": None},
        {"request_id": "r", "input_provenance": {"source": "cli"}},
        {
            "request_id": "r",
            "input_provenance": {
                "schema_version": "unknown",
                "source": "cli",
                "execution_request_id": "r",
            },
        },
        {
            "request_id": "r",
            "input_provenance": {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "source": "api",
                "execution_request_id": "r",
            },
        },
        {
            "request_id": "r",
            "input_provenance": {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "source": "cli",
                "execution_request_id": "other",
            },
        },
        {
            "request_id": "r",
            "input_provenance": {
                "schema_version": PROVENANCE_SCHEMA_VERSION,
                "source": "cli",
                "execution_request_id": "r",
                "extra": True,
            },
        },
    ],
)
def test_malformed_private_envelope_fails_with_one_stable_error(payload: object) -> None:
    with pytest.raises(ValueError, match="execution_context.provenance_invalid"):
        classify_execution_context_record(
            RunExecutionContextRecord(run_id="run-1", execution_context=cast(Any, payload))
        )


def test_missing_envelope_remains_legal_for_non_cli_and_legacy_runs() -> None:
    classified = classify_execution_context_record(
        RunExecutionContextRecord(
            run_id="legacy",
            execution_context={"request_id": None, "identity": {}},
        )
    )
    assert classified.provenance is None
    assert classified.authoritative_request_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provenance", "expected_source"),
    [(RunInputProvenance(source="cli"), "cli"), (None, None)],
)
async def test_guardrail_and_audit_use_only_typed_provenance(
    provenance: RunInputProvenance | None,
    expected_source: str | None,
) -> None:
    """业务 source 不参与来源判定；审计只记录受信 typed provenance。"""

    recorded: list[dict[str, Any]] = []

    class RecordingAudit:
        async def record(self, **kwargs: Any) -> SimpleNamespace:
            recorded.append(kwargs)
            return SimpleNamespace(id="audit-provenance")

    business_input = {"prompt": "ordinary", "source": "business-value"}
    guardrail = InputGuardrail(
        policy=PolicyEngine(provider=YamlPolicyProvider.default()),
        audit=cast(Any, RecordingAudit()),
    )
    result = await guardrail.check(
        actor=IdentityContext.local_default(),
        agent_id="fake-agent",
        input=business_input,
        provenance=provenance,
    )

    context = cast(dict[str, Any], result.metadata["context"])
    audit_payload = cast(dict[str, Any], recorded[0]["payload"])
    audit_context = cast(dict[str, Any], audit_payload["metadata"])["context"]
    assert business_input["source"] == "business-value"
    if expected_source is None:
        assert "source" not in context
        assert "source" not in audit_context
    else:
        assert context["source"] == expected_source
        assert audit_context["source"] == expected_source


def test_shipped_adapters_do_not_strip_business_source() -> None:
    root = Path("templates/service-app/agents/examples")
    violations: list[str] = []
    for path in sorted(root.glob("*/agent.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "pop"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "source"
            ):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []


def test_true_cli_run_keeps_source_out_of_business_input_and_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """离线 fake provider 足以证明 CLI 来源没有穿过业务 DTO 或 provider seam。"""

    dsn = sqlite_dsn(tmp_path / "cli-provenance.db")
    run_migrations(dsn)
    captured: list[dict[str, object]] = []
    original_complete = FakeModelProvider.complete

    async def capture(
        provider: FakeModelProvider,
        request: ModelRequest,
        *,
        plan: object,
    ) -> object:
        captured.append(cast(dict[str, object], request.model_dump(mode="json")))
        return await original_complete(provider, request, plan=plan)

    monkeypatch.setattr(FakeModelProvider, "complete", capture)
    result = CliRunner().invoke(
        runtime_cli.app,
        [
            "run",
            "examples.ticket_triage",
            "--profile",
            "local",
            "--profiles-dir",
            str(Path("templates/service-app/configs/profiles").resolve()),
            "--storage-dsn",
            dsn,
            "--events-path",
            str(tmp_path / "events.jsonl"),
            "--agents-dir",
            str(Path("templates/service-app/agents").resolve()),
            "--prompt",
            "billing invoice needs review",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "status: completed" in result.output
    assert len(captured) == 1
    serialized = json.dumps(captured[0], ensure_ascii=False, sort_keys=True)
    assert '"source"' not in serialized
    assert "provenance" not in serialized
