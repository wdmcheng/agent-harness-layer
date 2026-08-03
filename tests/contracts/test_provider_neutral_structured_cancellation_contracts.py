"""结构化调用取消、cleanup、崩溃恢复与候选漂移合同。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest
from tests.contracts.provider_neutral_structured_output_test_support import (
    assert_cancellation_resistant_cleanup_is_bounded,
)
from tests.contracts.test_provider_neutral_structured_transport_contracts import (
    BackoffCancellationRouter,
    ControlledStructuredProvider,
    CrashAfterStructuredSend,
    ShortStructuredDeadlineRouter,
    build_structured_bound,
    structured_request,
    structured_schema,
)

from agent_harness.models import ModelProviderInvocationError
from agent_harness.models._settlement_contracts import DurableMarkStateUnknown


@pytest.mark.asyncio
async def test_retryable_prepare_backoff_cancellation_is_durable_and_never_retries(
    tmp_path: Path,
) -> None:
    """Backoff期间取消须改写当前零请求attempt，并可从公开seam精确重放。"""

    schema = structured_schema()
    prepare_started = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        prepare_failures=1,
        prepare_started=prepare_started,
    )
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
        router_type=BackoffCancellationRouter,
    )
    try:
        invocation = asyncio.create_task(
            bound.complete_structured(
                structured_request(),
                operation_key="prepare-backoff-cancel",
            )
        )
        await prepare_started.wait()
        # Provider在set后同步抛出prepare错误，并在两秒backoff处首次让出控制权；
        # 此处取消因此不会与第二个transport ordinal竞态。
        await asyncio.sleep(0)
        invocation.cancel()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await invocation
        assert failure.value.code == "model.invocation_cancelled"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 1
        assert provider.prepares == 1
        assert provider.sends == []
        assert provider.closes == 0

        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            assert len(rows) == 1
            assert rows[0].result_json is not None
            durable_result = deepcopy(rows[0].result_json)
        attempts = durable_result["evidence"]["decision"]["attempts"]
        assert attempts[0]["structured_output"]["not_started_proof"]["kind"] == (
            "cancelled_before_send"
        )

        with pytest.raises(ModelProviderInvocationError) as replay:
            await bound.complete_structured(
                structured_request(),
                operation_key="prepare-backoff-cancel",
            )
        assert replay.value.code == "model.invocation_cancelled"
        assert replay.value.provider_called is False
        assert replay.value.attempt_count == 1
        assert provider.prepares == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancel_during_prepare_is_failed_without_send_or_fake_handle(tmp_path: Path) -> None:
    """Prepare 尚未返回时取消，provider 自清理局部状态且核心不伪造 close。"""

    schema = structured_schema()
    prepare_gate = asyncio.Event()
    prepare_started = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        prepare_gate=prepare_gate,
        prepare_started=prepare_started,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        task = asyncio.create_task(
            bound.complete_structured(
                structured_request(),
                operation_key="cancel-prepare",
            )
        )
        await prepare_started.wait()
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.invocation_cancelled"
        assert provider.sends == []
        assert provider.closes == 0
    finally:
        prepare_gate.set()
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancel_after_prepare_before_durable_send_mark_is_zero_request_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepared已返回但send mark未完成时取消，必须以核心发送前proof确定失败。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema)
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    mark_started = asyncio.Event()
    mark_gate = asyncio.Event()

    async def blocked_mark(**_kwargs: object) -> None:
        """把取消稳定放在prepared取得后、durable mark提交前的公开生命周期窗口。"""

        mark_started.set()
        await mark_gate.wait()

    monkeypatch.setattr(service, "_mark_side_effect_started", blocked_mark)
    try:
        task = asyncio.create_task(
            bound.complete_structured(
                structured_request(),
                operation_key="cancel-before-send-mark",
            )
        )
        await mark_started.wait()
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.invocation_cancelled"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 1
        assert provider.prepares == 1
        assert provider.sends == []
        assert provider.closes == 1
    finally:
        mark_gate.set()
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_durable_send_mark_state_unknown_before_send_is_needs_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable mark确认未知保留零请求事实，但不得把预算预约提前退款。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema)
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )

    async def unknown_mark(**_kwargs: object) -> None:
        """模拟生产mark取消信号；真实提交前后由shared-budget合同覆盖。"""

        raise DurableMarkStateUnknown

    monkeypatch.setattr(service, "_mark_side_effect_started", unknown_mark)
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                structured_request(),
                operation_key="durable-mark-unknown",
            )
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 1
        assert provider.sends == []
        assert provider.closes == 1
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_durable_send_mark_timeout_is_bounded_zero_request_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mark超时必须受总deadline约束，且不能伪造provider request。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema)
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
        router_type=ShortStructuredDeadlineRouter,
    )
    mark_gate = asyncio.Event()

    async def blocked_mark(**_kwargs: object) -> None:
        """模拟mark事务阻塞到调用总deadline，且无法确认commit是否发生。"""

        await mark_gate.wait()

    monkeypatch.setattr(service, "_mark_side_effect_started", blocked_mark)
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                structured_request(),
                operation_key="durable-mark-timeout",
            )
        assert failure.value.code == "model.provider_failed"
        assert failure.value.provider_called is False
        assert failure.value.attempt_count == 1
        assert provider.sends == []
        assert provider.closes == 1
    finally:
        mark_gate.set()
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancel_during_send_keeps_unknown_attempt_and_closes_once(tmp_path: Path) -> None:
    """Send 中取消不能启用第二 transport，且 cleanup 后仍为 needs-review。"""

    schema = structured_schema()
    send_gate = asyncio.Event()
    send_started = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        send_gate=send_gate,
        send_started=send_started,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        task = asyncio.create_task(
            bound.complete_structured(
                structured_request(),
                operation_key="cancel-send",
            )
        )
        await send_started.wait()
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        send_gate.set()
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancel_during_close_never_publishes_candidate_as_valid(tmp_path: Path) -> None:
    """Candidate 后 cleanup 被调用方取消时，受保护 close 完成但终态仍需复核。"""

    schema = structured_schema()
    close_gate = asyncio.Event()
    close_started = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        close_gate=close_gate,
        close_started=close_started,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        task = asyncio.create_task(
            bound.complete_structured(
                structured_request(),
                operation_key="cancel-close",
            )
        )
        await close_started.wait()
        task.cancel()
        close_gate.set()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        close_gate.set()
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_repeated_cancel_during_close_drains_prepared_cleanup_task(tmp_path: Path) -> None:
    """连续取消也必须在公开调用返回前终止prepared cleanup，不能遗留后台协程。"""

    schema = structured_schema()
    close_gate = asyncio.Event()
    close_started = asyncio.Event()
    close_finished = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        close_gate=close_gate,
        close_started=close_started,
        close_finished=close_finished,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    task = asyncio.create_task(
        bound.complete_structured(
            structured_request(),
            operation_key="repeated-cancel-close",
        )
    )
    try:
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "首次取消应继续在原deadline内保护prepared cleanup"
        task.cancel()
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
        await asyncio.wait_for(close_finished.wait(), timeout=0.1)
        assert close_gate.is_set() is False
    finally:
        close_gate.set()
        if not task.done():
            try:
                await task
            except BaseException:
                pass
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_blocked_prepared_cleanup_stops_at_single_deadline_and_needs_review(
    tmp_path: Path,
) -> None:
    """aclose永久阻塞也必须在冻结deadline内形成可恢复needs-review。"""

    schema = structured_schema()
    close_gate = asyncio.Event()
    close_started = asyncio.Event()
    provider = ControlledStructuredProvider(
        schema,
        close_gate=close_gate,
        close_started=close_started,
    )
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
        router_type=ShortStructuredDeadlineRouter,
    )
    task = asyncio.create_task(
        bound.complete_structured(
            structured_request(),
            operation_key="blocked-close-deadline",
        )
    )
    try:
        await close_started.wait()
        await asyncio.sleep(0.25)
        assert task.done(), "prepared cleanup escaped the frozen invocation deadline"
        with pytest.raises(ModelProviderInvocationError) as failure:
            await task
        assert failure.value.code == "model.provider_side_effect_unknown"
        assert failure.value.provider_called is True
        assert failure.value.attempt_count == 1
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1
    finally:
        close_gate.set()
        if not task.done():
            try:
                await task
            except BaseException:
                pass
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_cancellation_resistant_cleanup_is_bounded_and_explicitly_owned(
    tmp_path: Path,
) -> None:
    """cleanup吞掉取消也不得拖垮调用；未完成task必须由service显式持有。"""

    await assert_cancellation_resistant_cleanup_is_bounded(tmp_path)


@pytest.mark.asyncio
async def test_durable_started_after_crash_never_resends_or_fabricates_result(
    tmp_path: Path,
) -> None:
    """Mark 后崩溃只留下 started 围栏；恢复和 exact 调用都不得再次 send。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema, crash_after_send=True)
    service, storage, bound, run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        with pytest.raises(CrashAfterStructuredSend):
            await bound.complete_structured(
                structured_request(),
                operation_key="crash-after-send",
            )
        assert provider.sends == [(0, 1)]
        assert provider.closes == 1

        provider.crash_after_send = False
        recovered = await service.recover_pending(run_id=run_id)
        assert recovered == 1
        async with storage.uow() as uow:
            rows = await uow.evidence_outbox.list_for_run(run_id=run_id)
            durable_rows = [(row.state, row.result_json) for row in rows]
        assert len(durable_rows) == 1
        state, durable_result = durable_rows[0]
        assert state == "published"
        assert durable_result is not None
        summary = durable_result["evidence"]["decision"]["structured_output"]
        assert summary["status"] == "needs_review"
        assert summary["repair_count"] is None
        assert summary["provider_request_count"] is None
        assert summary["replay_identity"] is not None
        with pytest.raises(ModelProviderInvocationError) as replayed:
            await bound.complete_structured(
                structured_request(),
                operation_key="crash-after-send",
            )
        assert replayed.value.code == "model.provider_side_effect_unknown"
        assert provider.sends == [(0, 1)]
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_candidate_schema_identity_drift_maps_to_schema_invalid_and_exhausts_repair(
    tmp_path: Path,
) -> None:
    """错误 schema identity 使用稳定 schema_invalid，并受同一有限 repair 上限约束。"""

    schema = structured_schema()
    provider = ControlledStructuredProvider(schema, schema_identity_drift=True)
    service, storage, bound, _run_id = await build_structured_bound(
        tmp_path,
        provider=provider,
        schema=schema,
    )
    try:
        with pytest.raises(ModelProviderInvocationError) as failure:
            await bound.complete_structured(
                structured_request(),
                operation_key="schema-drift",
                repair_limit=1,
            )
        assert failure.value.code == "model.structured_repair_exhausted"
        assert provider.sends == [(0, 1), (1, 1)]
        assert provider.closes == 2
    finally:
        await service.aclose()
        await storage.dispose()


@pytest.mark.asyncio
async def test_candidate_provider_drift_and_missing_usage_are_needs_review(
    tmp_path: Path,
) -> None:
    """错误 provider identity 或不完整 sole usage 都不得发布 valid。"""

    cases = (
        ControlledStructuredProvider(structured_schema(), candidate_provider="provider-b"),
        ControlledStructuredProvider(structured_schema(), omit_usage=True),
    )
    for index, provider in enumerate(cases):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        service, storage, bound, _run_id = await build_structured_bound(
            case_dir,
            provider=provider,
            schema=provider.schema,
        )
        try:
            with pytest.raises(ModelProviderInvocationError) as failure:
                await bound.complete_structured(
                    structured_request(),
                    operation_key=f"candidate-drift-{index}",
                )
            assert failure.value.code == "model.provider_side_effect_unknown"
            assert provider.sends == [(0, 1)]
        finally:
            await service.aclose()
            await storage.dispose()
