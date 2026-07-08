## Source Links

- Product-Spec.md：`REQ-010`、`REQ-012`、`REQ-014`、`REQ-015`；terminal event、seq resume、payload_ref、guardrail/context 摘要和 local evidence 相关验收。
- DEV-PLAN.md：`Phase 4: CanonicalEvent、Artifact 与本地观测脊柱`；风险表中 prompt/tool output injection 和 event/observability boundary。
- 设计稿 / 架构图：`docs/architecture/pydantic-ai-agent-architecture.drawio` 中 Event Stream、Observability、Eval Gate 和信任边界。
- CONTEXT.md / ADR：当前仓库无。

## Why

runtime、eval、audit 和 service smoke 都需要同一套事件与证据格式。Phase 4 把 CanonicalEvent、artifact ref、local jsonl sink 和 OTel mapping facade 立住，避免后续每个 adapter 发明一套 trace payload。

## What Changes

- 新增 `CanonicalEvent` envelope、P0 event type、terminal event 和 per-run `seq` 规则。
- 新增 `EventBus` / `EventSink` interface、local jsonl sink 和断线续读 seam。
- 新增 artifact store，处理大 payload 的 `payload_ref`、checksum 和 metadata。
- 新增 secret redaction、guardrail/context assembly 摘要事件和 OTel mapping facade。
- 新增 SSE adapter 初版，为 Phase 5 run event stream API 复用。

## Non-Goals

- 不实现真实 OTel exporter、Logfire/Phoenix/Langfuse adapter、RAG retrieval、policy engine 或 eval gate。
- 不实现完整 run orchestration；Phase 5 只消费本 change 的 event bus 和 artifact store。

## Capabilities

### New Capabilities

- `canonical-events-artifacts`：CanonicalEvent、event bus/sink、local jsonl evidence、artifact store、redaction 和 OTel mapping facade。

### Modified Capabilities

- `core-contracts`：复用 trust/source/context ref 和 guardrail decision，不改变既有 DTO 行为。

## Impact

- 受影响代码：`packages/agent-harness/src/agent_harness/events/**`、`docs/architecture/**`、`security/**`、`observability/**`、`templates/service-app/app/api/sse.py`。
- 受影响测试：event stream contract tests、artifact store tests、redaction tests、SSE adapter smoke。
- 受影响数据：local jsonl traces 和 filesystem artifact payload。
