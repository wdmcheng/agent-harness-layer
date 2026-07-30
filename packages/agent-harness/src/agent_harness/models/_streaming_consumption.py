"""provider-neutral 流消费、跨块安全与公共分片。"""

from __future__ import annotations

from agent_harness.models._settlement_contracts import ModelProviderInvocationError
from agent_harness.models._streaming_contracts import StreamingRuntime
from agent_harness.models._streaming_events import persist_delta, publish_persisted_stream
from agent_harness.models.providers import ModelResponse, PreparedModelStreamCall
from agent_harness.models.router import ModelRoutePlan
from agent_harness.models.streaming import (
    MAX_STREAM_COLLECTOR_UTF8_BYTES,
    IncrementalTextGuard,
    StreamLimitExceeded,
    Utf8TextChunker,
)
from agent_harness.models.usage import UsageEvidenceContext
from agent_harness.storage.shared_budget import BudgetOperationOwnership


async def consume_prepared_stream(
    runtime: StreamingRuntime,
    *,
    prepared: PreparedModelStreamCall,
    context: UsageEvidenceContext,
    usage_call_id: str,
    ownership: BudgetOperationOwnership | None,
    plan: ModelRoutePlan,
    chunks: list[str],
) -> ModelResponse:
    """在冻结总 deadline 内拉取、保护、持久化并完成一个 provider stream。"""

    await runtime.mark_side_effect_started(
        context=context,
        usage_call_id=usage_call_id,
        ownership=ownership,
    )
    raw_fragments: list[str] = []
    raw_utf8_bytes = 0
    guard = IncrementalTextGuard(
        max_candidate_utf8_bytes=runtime.router.stream_sensitive_candidate_utf8_bytes
    )
    chunker = Utf8TextChunker(target_utf8_bytes=runtime.router.stream_chunk_utf8_bytes)
    full_result_mode = runtime.output_guardrail is not None
    async for fragment in prepared:
        if runtime.timing_observer is not None:
            runtime.timing_observer("provider_delta")
        next_size = raw_utf8_bytes + fragment.utf8_bytes
        if next_size > MAX_STREAM_COLLECTOR_UTF8_BYTES:
            raise StreamLimitExceeded
        raw_utf8_bytes = next_size
        raw_fragments.append(fragment.text)
        if full_result_mode:
            continue
        await _feed_safe_text(
            runtime,
            guard=guard,
            chunker=chunker,
            text=fragment.text,
            context=context,
            usage_call_id=usage_call_id,
            chunks=chunks,
        )
    provider_response = await prepared.result()
    response = ModelResponse.model_validate(provider_response.model_dump(mode="python"))
    response = runtime.router.normalize_response(response, plan=plan)
    raw_text = "".join(raw_fragments)
    if raw_text != response.output_text:
        # 最终结果与已经观察、持久化的增量冲突后，adapter 的停止/完整计量证明也
        # 不能恢复内容一致性；必须强制进入 unknown，保留全部未决围栏供人工处置。
        raise ModelProviderInvocationError(
            "model.provider_side_effect_unknown",
            provider_called=True,
            attempt_count=1,
            latency_ms=response.latency_ms,
            failure_domain="runtime",
        )
    if runtime.output_guardrail is not None:
        if not runtime.output_guardrail(raw_text):
            raise ModelProviderInvocationError(
                "model.provider_failed",
                provider_called=True,
                attempt_count=1,
                latency_ms=response.latency_ms,
                failure_domain="runtime",
            )
        await _feed_safe_text(
            runtime,
            guard=guard,
            chunker=chunker,
            text=raw_text,
            context=context,
            usage_call_id=usage_call_id,
            chunks=chunks,
        )
    for safe_part in guard.finish():
        await _feed_chunks(
            runtime,
            chunker=chunker,
            text=safe_part,
            context=context,
            usage_call_id=usage_call_id,
            chunks=chunks,
        )
    for chunk in chunker.finish():
        intent = await persist_delta(
            runtime,
            context=context,
            usage_call_id=usage_call_id,
            ordinal=len(chunks) + 1,
            text=chunk,
        )
        chunks.append(chunk)
        await publish_persisted_stream(runtime, intent)
    return response.model_copy(update={"output_text": "".join(chunks)})


async def _feed_safe_text(
    runtime: StreamingRuntime,
    *,
    guard: IncrementalTextGuard,
    chunker: Utf8TextChunker,
    text: str,
    context: UsageEvidenceContext,
    usage_call_id: str,
    chunks: list[str],
) -> None:
    """把连续文本经有状态安全处理后串行交给公共分片器。"""

    for safe_part in guard.feed(text):
        await _feed_chunks(
            runtime,
            chunker=chunker,
            text=safe_part,
            context=context,
            usage_call_id=usage_call_id,
            chunks=chunks,
        )


async def _feed_chunks(
    runtime: StreamingRuntime,
    *,
    chunker: Utf8TextChunker,
    text: str,
    context: UsageEvidenceContext,
    usage_call_id: str,
    chunks: list[str],
) -> None:
    """逐条持久化安全分片，返回前不允许拉取下一个 provider 事件。"""

    for chunk in chunker.feed(text):
        intent = await persist_delta(
            runtime,
            context=context,
            usage_call_id=usage_call_id,
            ordinal=len(chunks) + 1,
            text=chunk,
        )
        chunks.append(chunk)
        await publish_persisted_stream(runtime, intent)


__all__ = ["consume_prepared_stream"]
