"""受控模型文本流的独立 live smoke schema、时钟与默认零网络合同。"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from scripts.smoke_live_model_stream import (
    LiveStreamSmokeExecutor,
    StreamTimingRecorder,
    main,
    make_result,
    measure_existing_sse_first_frame,
    run,
    validate_result,
)

from agent_harness.events import CanonicalEvent, CanonicalEventType, LocalJsonlEventSink
from agent_harness.identity import IdentityContext
from agent_harness.models import (
    ModelDecision,
    ModelProviderInvocationError,
    ModelRequest,
    ModelResponse,
)
from agent_harness.runtime import AgentExecutionContext, AgentExecutionRequest, RunStatus
from scripts import live_model_stream_execution, smoke_live_model_stream

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "templates" / "service-app" / "configs" / "profiles"


@pytest.mark.asyncio
async def test_default_stream_smoke_is_hosted_unverified_and_never_opens_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺本会话授权时必须在配置、credential 与 socket 之前安全退出。"""

    monkeypatch.delenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", raising=False)
    monkeypatch.delenv("AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-must-not-be-read")

    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("默认 stream smoke 不得联网")

    monkeypatch.setattr(socket, "create_connection", blocked)
    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 0
    assert payload == {
        "schema_version": "model-stream-live-smoke/v1",
        "status": "hosted-unverified",
        "provider_called": False,
        "existing_event_first_frame_ms": None,
        "provider_first_delta_ms": None,
        "committed_first_delta_ms": None,
        "client_first_delta_ms": None,
        "reason_code": "authorization_missing",
    }


@pytest.mark.asyncio
async def test_stream_opt_in_is_an_independent_second_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非流式授权不能隐式授权 streaming；独立 opt-in 缺失仍零调用。"""

    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", "1")
    monkeypatch.delenv("AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN", raising=False)
    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 0
    assert payload["status"] == "hosted-unverified"
    assert payload["reason_code"] == "stream_opt_in_missing"
    assert payload["provider_called"] is False


def test_stream_timing_recorder_uses_one_origin_and_preserves_first_observation() -> None:
    """provider/committed/client 三项共用同一 monotonic origin，重复阶段不覆盖首次值。"""

    ticks = iter([10.000, 10.007, 10.011, 10.019, 99.000])
    recorder = StreamTimingRecorder(clock=lambda: next(ticks))
    recorder.observe("origin")
    recorder.observe("provider_delta")
    recorder.observe("committed_delta")
    recorder.observe("client_delta")
    recorder.observe("provider_delta")

    assert recorder.provider_first_delta_ms == 7
    assert recorder.committed_first_delta_ms == 11
    assert recorder.client_first_delta_ms == 19


@pytest.mark.asyncio
async def test_existing_event_latency_is_measured_from_an_actual_sse_first_frame() -> None:
    """独立指标必须驱动 ASGI SSE request，并在第一段 frame 到达时停止计时。"""

    observed_scope: dict[str, Any] = {}

    async def app(
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        observed_scope.update(scope)
        assert (await receive())["type"] == "http.request"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"id: 1\nevent: run.started\ndata: {}\n\n",
                "more_body": False,
            }
        )

    ticks = iter([20.000, 20.006])
    elapsed = await measure_existing_sse_first_frame(
        cast(Any, app),
        run_id="run-live-stream",
        clock=lambda: next(ticks),
    )

    assert elapsed == 6
    assert observed_scope["method"] == "GET"
    assert observed_scope["path"] == "/api/v1/runs/run-live-stream/events/stream"
    assert (b"accept", b"text/event-stream") in observed_scope["headers"]


def test_stream_live_result_schema_and_order_are_exact() -> None:
    """成功、skip 与外部阻断只接受封闭字段、非负整数和真实时序。"""

    passed = make_result(
        status="passed",
        provider_called=True,
        existing_event_first_frame_ms=3,
        provider_first_delta_ms=7,
        committed_first_delta_ms=11,
        client_first_delta_ms=19,
        reason_code=None,
    )
    assert validate_result(passed) == passed
    assert (
        validate_result(
            make_result(
                status="hosted-unverified",
                provider_called=False,
                reason_code="credential_missing",
            )
        )["status"]
        == "hosted-unverified"
    )
    assert (
        validate_result(
            make_result(
                status="external-blocked",
                provider_called=True,
                provider_first_delta_ms=7,
                committed_first_delta_ms=11,
                reason_code="provider_result_unknown",
            )
        )["status"]
        == "external-blocked"
    )

    for invalid in (
        {**passed, "provider_first_delta_ms": True},
        {**passed, "committed_first_delta_ms": 6},
        {**passed, "reason_code": "completed"},
        {**passed, "prompt": "forbidden"},
    ):
        with pytest.raises(ValueError):
            validate_result(invalid)


def test_stream_live_contract_failure_preserves_observed_provider_evidence() -> None:
    """provider 已返回后发生本地终态失败时，不得伪装成外部阻断或零调用。"""

    payload, exit_code = smoke_live_model_stream.classify_incomplete_run(
        response_observed=True,
        error=None,
        existing_event_first_frame_ms=None,
        provider_first_delta_ms=7,
        committed_first_delta_ms=11,
        client_first_delta_ms=19,
    )

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "contract_failure"
    assert payload["provider_called"] is True

    partial_payload, partial_exit_code = smoke_live_model_stream.classify_incomplete_run(
        response_observed=False,
        error=None,
        existing_event_first_frame_ms=None,
        provider_first_delta_ms=7,
        committed_first_delta_ms=11,
        client_first_delta_ms=None,
    )
    assert partial_exit_code == 1
    assert partial_payload["status"] == "failed"
    assert partial_payload["provider_called"] is True


def test_stream_live_incomplete_run_only_classifies_provider_errors_as_external() -> None:
    """本地 bulkhead 是 contract failure；provider 稳定错误才是外部阻断。"""

    local_payload, local_exit_code = smoke_live_model_stream.classify_incomplete_run(
        response_observed=False,
        error=ModelProviderInvocationError(
            "model.bulkhead_saturated",
            failure_domain="runtime",
        ),
        existing_event_first_frame_ms=None,
        provider_first_delta_ms=None,
        committed_first_delta_ms=None,
        client_first_delta_ms=None,
    )
    assert (local_payload["status"], local_payload["provider_called"], local_exit_code) == (
        "failed",
        False,
        1,
    )

    external_payload, external_exit_code = smoke_live_model_stream.classify_incomplete_run(
        response_observed=False,
        error=ModelProviderInvocationError(
            "model.provider_side_effect_unknown",
            provider_called=True,
            attempt_count=1,
        ),
        existing_event_first_frame_ms=None,
        provider_first_delta_ms=None,
        committed_first_delta_ms=None,
        client_first_delta_ms=None,
    )
    assert (
        external_payload["status"],
        external_payload["reason_code"],
        external_payload["provider_called"],
        external_exit_code,
    ) == ("external-blocked", "provider_result_unknown", True, 2)


@pytest.mark.asyncio
async def test_stream_live_executor_preserves_runtime_failure_domain(
    tmp_path: Path,
) -> None:
    """真实 executor 捕获本地 guardrail 类错误后仍生成 contract failure 证据。"""

    ticks = iter([10.000, 10.007])
    recorder = StreamTimingRecorder(clock=lambda: next(ticks))

    class RuntimeFailingInvocation:
        async def stream(self, *_args: object, **_kwargs: object) -> None:
            recorder.observe("origin")
            recorder.observe("provider_delta")
            raise ModelProviderInvocationError(
                "model.provider_failed",
                provider_called=True,
                attempt_count=1,
                latency_ms=7,
                failure_domain="runtime",
            )

    executor = LiveStreamSmokeExecutor(
        request=ModelRequest(
            capability="text_stream",
            prompt="fixed smoke prompt",
            max_output_tokens=8,
        ),
        sink=LocalJsonlEventSink(tmp_path / "stream-runtime-failure.jsonl"),
        recorder=recorder,
    )
    context = AgentExecutionContext(identity=IdentityContext.local_default()).bind_services(
        {"model_invocation": RuntimeFailingInvocation()}
    )
    result = await executor.run(
        AgentExecutionRequest(
            agent_id="system.live_model_stream_smoke",
            run_id="run-live-stream-failure",
            input={},
        ),
        context,
    )

    assert result.status == RunStatus.FAILED.value
    payload, exit_code = smoke_live_model_stream.classify_incomplete_run(
        response_observed=executor.response is not None,
        error=executor.error,
        existing_event_first_frame_ms=None,
        provider_first_delta_ms=recorder.provider_first_delta_ms,
        committed_first_delta_ms=recorder.committed_first_delta_ms,
        client_first_delta_ms=recorder.client_first_delta_ms,
    )
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "contract_failure"
    assert payload["provider_called"] is True


@pytest.mark.asyncio
async def test_stream_live_full_run_closes_local_setup_failure_into_safe_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整受控执行在 migration 等本地编排失败时仍必须返回封闭 JSON。"""

    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", "1")
    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN", "1")
    settings = SimpleNamespace(
        model=SimpleNamespace(default_deployment_id="authorized-deployment"),
    )
    resolved = SimpleNamespace(
        credential="isolated-test-reference",
        provider_kind="openai-compatible",
        endpoint_origin="https://trusted.invalid",
        allowed_models=("model-a",),
        default_model="model-a",
    )

    def fake_load_settings(**_kwargs: object) -> SimpleNamespace:
        return settings

    def fake_resolve_model_deployment(*_args: object) -> SimpleNamespace:
        return resolved

    monkeypatch.setattr(live_model_stream_execution, "load_settings", fake_load_settings)
    monkeypatch.setattr(
        live_model_stream_execution,
        "resolve_model_deployment",
        fake_resolve_model_deployment,
    )

    def fail_migration(_dsn: str) -> None:
        raise RuntimeError("local migration details must not escape")

    monkeypatch.setattr(live_model_stream_execution, "run_migrations", fail_migration)

    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 1
    assert payload == {
        "schema_version": "model-stream-live-smoke/v1",
        "status": "failed",
        "provider_called": False,
        "existing_event_first_frame_ms": None,
        "provider_first_delta_ms": None,
        "committed_first_delta_ms": None,
        "client_first_delta_ms": None,
        "reason_code": "contract_failure",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_stream_live_local_start_run_failure_overrides_provider_domain_classification(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    """provider 错误后的本地 start_run 失败必须覆盖外部归因，与 cleanup 结果无关。"""

    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_AUTHORIZED", "1")
    monkeypatch.setenv("AGENT_HARNESS_LIVE_MODEL_STREAM_OPT_IN", "1")
    settings = SimpleNamespace(
        model=SimpleNamespace(default_deployment_id="authorized-deployment"),
        budget=SimpleNamespace(max_tokens_per_run=8, max_cost_usd_per_run=1.0),
        identity=SimpleNamespace(default=IdentityContext.local_default()),
    )
    resolved = SimpleNamespace(
        credential="isolated-test-reference",
        provider_kind="openai-compatible",
        endpoint_origin="https://trusted.invalid",
        allowed_models=("model-a",),
        default_model="model-a",
    )

    class StorageDouble:
        async def dispose(self) -> None:
            """正常路径不会走到这里；cleanup 替身在更外层强制失败。"""

    class OrchestratorDouble:
        def __init__(self, **kwargs: object) -> None:
            self._resolver = cast(
                Callable[[str], LiveStreamSmokeExecutor],
                kwargs["executor_resolver"],
            )

        async def start_run(self, **_kwargs: object) -> None:
            executor = self._resolver("system.live_model_stream_smoke")
            executor.error = ModelProviderInvocationError(
                "model.provider_failed",
                provider_called=True,
                attempt_count=1,
                latency_ms=7,
            )
            raise RuntimeError("provider failure already normalized")

    async def close_runtime(**_kwargs: object) -> None:
        if cleanup_fails:
            raise RuntimeError("local cleanup details must not escape")

    def fake_load_settings(**_kwargs: object) -> object:
        return settings

    def fake_resolve_model_deployment(*_args: object) -> object:
        return resolved

    def skip_migrations(_dsn: str) -> None:
        return None

    def fake_storage(_dsn: str) -> Any:
        return StorageDouble()

    def fake_services(**_kwargs: object) -> dict[str, object]:
        return {}

    monkeypatch.setattr(live_model_stream_execution, "load_settings", fake_load_settings)
    monkeypatch.setattr(
        live_model_stream_execution,
        "resolve_model_deployment",
        fake_resolve_model_deployment,
    )
    monkeypatch.setattr(live_model_stream_execution, "run_migrations", skip_migrations)
    monkeypatch.setattr(
        live_model_stream_execution.SQLAlchemyStorage,
        "from_dsn",
        fake_storage,
    )
    monkeypatch.setattr(
        live_model_stream_execution,
        "build_agent_execution_services",
        fake_services,
    )
    monkeypatch.setattr(live_model_stream_execution, "RunOrchestrator", OrchestratorDouble)
    monkeypatch.setattr(live_model_stream_execution, "_close_runtime", close_runtime)

    payload, exit_code = await run(profile="service", profiles_dir=PROFILES)

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "contract_failure"
    assert payload["provider_called"] is True


@pytest.mark.asyncio
async def test_stream_live_client_observation_waits_for_committed_timing_boundary(
    tmp_path: Path,
) -> None:
    """并发 reader 先看见 durable delta 时，client 时钟仍须发生在 commit 记录后。"""

    ticks = iter([10.000, 10.005, 10.006, 10.007, 10.008])
    recorder = StreamTimingRecorder(clock=lambda: next(ticks))

    async def resolve_trace(*, tenant_id: str, run_id: str) -> str:
        assert (tenant_id, run_id) == ("local", "run-live-stream-order")
        return "trace-live-stream-order"

    sink = LocalJsonlEventSink(
        tmp_path / "stream-timing-order.jsonl",
        run_trace_resolver=resolve_trace,
    )

    class VisibleBeforeObserverInvocation:
        async def stream(self, *_args: object, **_kwargs: object) -> ModelResponse:
            recorder.observe("origin")
            recorder.observe("provider_delta")
            await sink.write(
                CanonicalEvent(
                    event_id="live-stream-timing-delta",
                    tenant_id="local",
                    run_id="run-live-stream-order",
                    agent_id="system.live_model_stream_smoke",
                    event_type=CanonicalEventType.MODEL_OUTPUT_DELTA,
                    seq=0,
                    payload={
                        "correlation": {"usage_call_id": "a" * 64},
                        "attempt": 1,
                        "chunk_ordinal": 1,
                        "text": "OK",
                    },
                    visibility="public",
                    request_id="request-live-stream-order",
                    trace_id="trace-live-stream-order",
                )
            )
            await asyncio.sleep(0)
            recorder.observe("committed_delta")
            return ModelResponse(
                provider="fake",
                model="fake-stream",
                output_text="OK",
                decision=ModelDecision(action="call", estimated_tokens=2),
                token_usage={"input_tokens": 1, "output_tokens": 1},
                latency_ms=7,
            )

    executor = LiveStreamSmokeExecutor(
        request=ModelRequest(
            capability="text_stream",
            prompt="fixed smoke prompt",
            max_output_tokens=8,
        ),
        sink=sink,
        recorder=recorder,
    )
    context = AgentExecutionContext(identity=IdentityContext.local_default()).bind_services(
        {"model_invocation": VisibleBeforeObserverInvocation()}
    )

    result = await executor.run(
        AgentExecutionRequest(
            agent_id="system.live_model_stream_smoke",
            run_id="run-live-stream-order",
            input={},
        ),
        context,
    )

    assert result.status == RunStatus.COMPLETED.value
    assert recorder.provider_first_delta_ms == 5
    assert recorder.committed_first_delta_ms == 6
    assert recorder.client_first_delta_ms == 7


def test_stream_live_cli_closes_artifact_write_failure_into_safe_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """artifact 写入失败时 CLI 仍只输出封闭失败 JSON，不泄漏本地异常。"""

    async def fake_run(**_kwargs: object) -> tuple[dict[str, object], int]:
        return make_result(
            status="passed",
            provider_called=True,
            existing_event_first_frame_ms=3,
            provider_first_delta_ms=7,
            committed_first_delta_ms=11,
            client_first_delta_ms=19,
            reason_code=None,
        ), 0

    def fail_write_text(
        _path: Path,
        _data: str,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        del encoding, errors, newline
        raise OSError("private artifact path details")

    monkeypatch.setattr(smoke_live_model_stream, "run", fake_run)
    monkeypatch.setattr(Path, "write_text", fail_write_text)
    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke_live_model_stream.py", "--output", str(tmp_path / "result.json")],
    )

    assert main() == 1
    rendered = capsys.readouterr().out.strip()
    payload = json.loads(rendered)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "contract_failure"
    assert payload["provider_called"] is True
    assert "private artifact path details" not in rendered
